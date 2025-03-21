
"""Simple code for training an RNN for motion prediction."""
# Mainly adopted from https://github.com/una-dinosauria/human-motion-prediction

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import h5py

import numpy as np
from six.moves import xrange # pylint: disable=redefined-builtin
import tensorflow as tf

import data_utils
import prediction_model


# Learning
tf.app.flags.DEFINE_float("learning_rate", .05, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.95, "Learning rate is multiplied by this much. 1 means no decay.")
tf.app.flags.DEFINE_integer("learning_rate_step", 10000, "Every this many steps, do decay.")
tf.app.flags.DEFINE_float("max_gradient_norm", 1, "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer("batch_size", 16, "Batch size to use during training.")
tf.app.flags.DEFINE_integer("iterations", 50000, "Iterations to train for.")
# Architecture
tf.app.flags.DEFINE_integer("size", 64, "Size of each model layer.")
tf.app.flags.DEFINE_integer("num_layers", 1, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("seq_length_in", 50, "Number of frames to feed into the encoder. 25 fps")
tf.app.flags.DEFINE_integer("seq_length_out", 25, "Number of frames that the decoder has to predict. 25fps")
tf.app.flags.DEFINE_integer("max_diffusion_step", 3, "Number of maximum diffusion steps in the model.")
tf.app.flags.DEFINE_string("filter_type", "dual_random_walk", "laplacian/random_walk/dual_random_walk")
tf.app.flags.DEFINE_boolean("omit_one_hot", True, "Whether to remove one-hot encoding from the data")
tf.app.flags.DEFINE_boolean("train_on_euler", False, "Train using euler angle")
tf.app.flags.DEFINE_boolean("velocity", True, "Train using velocity")
tf.app.flags.DEFINE_string("action","all", "The action to train on. all means all the actions, all_periodic means walking, eating and smoking")
# Directories
tf.app.flags.DEFINE_string("data_dir", os.path.normpath("./data/h3.6m/dataset"), "Data directory")
tf.app.flags.DEFINE_string("train_dir", os.path.normpath("./experiments/"), "Training directory.")
# Evaluations
tf.app.flags.DEFINE_boolean("eval_pose", True, "Training evaluation on pose")
tf.app.flags.DEFINE_integer("test_every", 1000, "How often to compute error on the test set.")
tf.app.flags.DEFINE_integer("save_every", 1000, "How often to compute error on the test set.")
tf.app.flags.DEFINE_boolean("sample", False, "Set to True for sampling.")
tf.app.flags.DEFINE_boolean("use_cpu", False, "Whether to use the CPU")
tf.app.flags.DEFINE_integer("load", 0, "Try to load a previous checkpoint.")

FLAGS = tf.app.flags.FLAGS

train_dir = os.path.normpath(os.path.join( FLAGS.train_dir, FLAGS.action,
  'out_{0}'.format(FLAGS.seq_length_out),
  'iterations_{0}'.format(FLAGS.iterations),
  'omit_one_hot' if FLAGS.omit_one_hot else 'one_hot',
  'size_{0}'.format(FLAGS.size),
  'lr_{0}'.format(FLAGS.learning_rate)))

summaries_dir = os.path.normpath(os.path.join( train_dir, "log" )) # Directory for TB summaries

def create_model(session, actions, sampling=False):
  """Create translation model and initialize or load parameters in session."""

  model = prediction_model.Seq2SeqModel(
      FLAGS.seq_length_in if not sampling else 50,
      FLAGS.seq_length_out if not sampling else 25,
      FLAGS.size, # hidden layer size
      FLAGS.num_layers,
      FLAGS.max_gradient_norm,
      FLAGS.batch_size,
      FLAGS.learning_rate,
      FLAGS.learning_rate_decay_factor,
      summaries_dir,
      len( actions ),
      FLAGS.max_diffusion_step,
      FLAGS.filter_type,
      not FLAGS.omit_one_hot,
      FLAGS.eval_pose,
      dtype=tf.float32)

  if FLAGS.load <= 0:
    print("Creating model with fresh parameters.")
    session.run(tf.global_variables_initializer())
    return model

  ckpt = tf.train.get_checkpoint_state( train_dir, latest_filename="checkpoint")
  print( "train_dir", train_dir )

  if ckpt and ckpt.model_checkpoint_path:
    # Check if the specific checkpoint exists
    if FLAGS.load > 0:
      if os.path.isfile(os.path.join(train_dir,"checkpoint-{0}.index".format(FLAGS.load))):
        ckpt_name = os.path.normpath(os.path.join( os.path.join(train_dir,"checkpoint-{0}".format(FLAGS.load)) ))
      else:
        raise ValueError("Asked to load checkpoint {0}, but it does not seem to exist".format(FLAGS.load))
    else:
      ckpt_name = os.path.basename( ckpt.model_checkpoint_path )

    print("Loading model {0}".format( ckpt_name ))
    model.saver.restore( session, ckpt_name )
    return model
  else:
    print("Could not find checkpoint. Aborting.")
    raise( ValueError, "Checkpoint {0} does not seem to exist".format( ckpt.model_checkpoint_path ) )

  return model


def train():
  """Train a seq2seq model on human motion"""

  actions = define_actions( FLAGS.action )

  number_of_actions = len( actions )

  train_set, test_set, data_mean, data_std, dim_to_ignore, dim_to_use = read_all_data(
    actions, FLAGS.seq_length_in, FLAGS.seq_length_out, FLAGS.data_dir, not FLAGS.omit_one_hot, FLAGS.train_on_euler )

  # Limit TF to take a fraction of the GPU memory
  gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=1)
  device_count = {"GPU": 0} if FLAGS.use_cpu else {"GPU": 1}

  with tf.Session(config=tf.ConfigProto( gpu_options=gpu_options, device_count = device_count )) as sess:

    # === Create the model ===
    print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))

    model = create_model( sess, actions )
    model.train_writer.add_graph( sess.graph )
    print( "Model created" )

    # === Read and denormalize the gt with srnn's seeds, as we'll need them
    # many times for evaluation in Euler Angles ===
    srnn_gts_euler = get_srnn_gts( actions, model, test_set, data_mean,
                              data_std, dim_to_ignore, not FLAGS.omit_one_hot, from_exp=not FLAGS.train_on_euler )

    #=== This is the training loop ===
    current_step = 0 if FLAGS.load <= 0 else FLAGS.load + 1


    for _ in xrange( FLAGS.iterations ):

      # === Training step ===
      action_prefix, action_postfix_input, action_postfix_output, action_poses = model.get_batch(train_set, not FLAGS.omit_one_hot)

      _, step_mse_fw_loss, step_mse_bw_loss, mse_loss_summary, lr_summary = model.step( sess, action_prefix[:,:,6:], action_postfix_input[:,:,6:], action_postfix_output[:,:,6:], action_poses[:,:,6:], False )
      model.train_writer.add_summary( mse_loss_summary, current_step)
      model.train_writer.add_summary( lr_summary, current_step )

      if current_step % 10 == 0:
        print("step {0:04d}; mse_loss_fw: {1:.4f}; mse_loss_bw: {2:.4f}".format(current_step, step_mse_fw_loss, step_mse_bw_loss))
      current_step += 1

      # === step decay ===
      if current_step % FLAGS.learning_rate_step == 0:
        sess.run(model.learning_rate_decay_op)

      # Once in a while, we save checkpoint, print statistics, and run evals.
      if current_step % FLAGS.test_every == 0:

        # === Validation with randomly chosen seeds ===
        forward_only = True

        action_prefix, action_postfix_input, action_postfix_output, action_poses = model.get_batch(train_set, not FLAGS.omit_one_hot)
        step_mse_loss_fw, mse_loss_summary = model.step(sess, action_prefix[:,:,6:], action_postfix_input[:,:,6:], action_postfix_output[:,:,6:], action_poses[:,:,6:], forward_only)
        val_mse_loss_fw = step_mse_loss_fw
        model.test_writer.add_summary(mse_loss_summary, current_step)

        print()
        print("{0: <16} |".format("milliseconds"), end="")
        for ms in [80, 160, 320, 400, 560, 1000]:
          print(" {0:5d} |".format(ms), end="")
        print()

        # === Validation with srnn's seeds ===
        for action in actions:

          # Evaluate the model on the test batches
          action_prefix, action_postfix_input, action_postfix_output, action_poses = model.get_batch_srnn( test_set, action, FLAGS.velocity )

          srnn_mse_loss_fw, _, srnn_poses = model.step(sess, action_prefix[:,:,6:], action_postfix_input[:,:,6:], action_postfix_output[:,:,6:], action_poses[:,:,6:], True, True)
          srnn_poses = np.concatenate((np.transpose(action_postfix_output[:, :, :6], [1, 0, 2]), srnn_poses), axis=-1)
          # Denorm the output
          srnn_pred = data_utils.revert_output_format( srnn_poses, action_poses[:,FLAGS.seq_length_in-1,:],
            data_mean, data_std, dim_to_ignore, actions, not FLAGS.omit_one_hot, FLAGS.velocity )

          # Save the errors here
          mean_errors = np.zeros( (len(srnn_pred), srnn_pred[0].shape[0]) )

          # Training is done in exponential map, but the error is reported in
          # Euler angles, as in previous work.
          # See https://github.com/asheshjain399/RNNexp/issues/6#issuecomment-247769197
          N_SEQUENCE_TEST = 8
          for i in np.arange(N_SEQUENCE_TEST):
            eulerchannels_pred = srnn_pred[i]

            if not FLAGS.train_on_euler:
                # Convert from exponential map to Euler angles
                for j in np.arange( eulerchannels_pred.shape[0] ):
                  for k in np.arange(3,97,3):
                    eulerchannels_pred[j,k:k+3] = data_utils.rotmat2euler(
                      data_utils.expmap2rotmat( eulerchannels_pred[j,k:k+3] ))

            # The global translation (first 3 entries) and global rotation
            # (next 3 entries) are also not considered in the error, so the_key
            # are set to zero.
            # See https://github.com/asheshjain399/RNNexp/issues/6#issuecomment-249404882
            gt_i=np.copy(srnn_gts_euler[action][i])
            gt_i[:,0:6] = 0

            # Now compute the l2 error. The following is numpy port of the error
            # function provided by Ashesh Jain (in matlab), available at
            # https://github.com/asheshjain399/RNNexp/blob/srnn/structural_rnn/CRFProblems/H3.6m/dataParser/Utils/motionGenerationError.m#L40-L54
            idx_to_use = np.where( np.std( gt_i, 0 ) > 1e-4 )[0]
            
            euc_error = np.power( gt_i[:,idx_to_use] - eulerchannels_pred[:,idx_to_use], 2)
            euc_error = np.sum(euc_error, 1)
            euc_error = np.sqrt( euc_error )
            mean_errors[i,:] = euc_error

          # This is simply the mean error over the N_SEQUENCE_TEST examples
          mean_mean_errors = np.mean( mean_errors, 0 )

          # Pretty print of the results for 80, 160, 320, 400, 560 and 1000 ms
          print("{0: <16} |".format(action), end="")
          for ms in [1,3,7,9,13,24]:
            if FLAGS.seq_length_out >= ms+1:
              print(" {0:.3f} |".format( mean_mean_errors[ms] ), end="")
            else:
              print("   n/a |", end="")
          print()

          # Ugly massive if-then to log the error to tensorboard :shrug:
          if action == "walking":
            summaries = sess.run(
              [model.walking_err80_summary,
               model.walking_err160_summary,
               model.walking_err320_summary,
               model.walking_err400_summary,
               model.walking_err560_summary,
               model.walking_err1000_summary],
              {model.walking_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.walking_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.walking_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.walking_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.walking_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.walking_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "eating":
            summaries = sess.run(
              [model.eating_err80_summary,
               model.eating_err160_summary,
               model.eating_err320_summary,
               model.eating_err400_summary,
               model.eating_err560_summary,
               model.eating_err1000_summary],
              {model.eating_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.eating_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.eating_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.eating_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.eating_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.eating_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "smoking":
            summaries = sess.run(
              [model.smoking_err80_summary,
               model.smoking_err160_summary,
               model.smoking_err320_summary,
               model.smoking_err400_summary,
               model.smoking_err560_summary,
               model.smoking_err1000_summary],
              {model.smoking_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.smoking_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.smoking_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.smoking_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.smoking_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.smoking_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "discussion":
            summaries = sess.run(
              [model.discussion_err80_summary,
               model.discussion_err160_summary,
               model.discussion_err320_summary,
               model.discussion_err400_summary,
               model.discussion_err560_summary,
               model.discussion_err1000_summary],
              {model.discussion_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.discussion_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.discussion_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.discussion_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.discussion_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.discussion_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "directions":
            summaries = sess.run(
              [model.directions_err80_summary,
               model.directions_err160_summary,
               model.directions_err320_summary,
               model.directions_err400_summary,
               model.directions_err560_summary,
               model.directions_err1000_summary],
              {model.directions_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.directions_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.directions_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.directions_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.directions_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.directions_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "greeting":
            summaries = sess.run(
              [model.greeting_err80_summary,
               model.greeting_err160_summary,
               model.greeting_err320_summary,
               model.greeting_err400_summary,
               model.greeting_err560_summary,
               model.greeting_err1000_summary],
              {model.greeting_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.greeting_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.greeting_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.greeting_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.greeting_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.greeting_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "phoning":
            summaries = sess.run(
              [model.phoning_err80_summary,
               model.phoning_err160_summary,
               model.phoning_err320_summary,
               model.phoning_err400_summary,
               model.phoning_err560_summary,
               model.phoning_err1000_summary],
              {model.phoning_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.phoning_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.phoning_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.phoning_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.phoning_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.phoning_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "posing":
            summaries = sess.run(
              [model.posing_err80_summary,
               model.posing_err160_summary,
               model.posing_err320_summary,
               model.posing_err400_summary,
               model.posing_err560_summary,
               model.posing_err1000_summary],
              {model.posing_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.posing_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.posing_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.posing_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.posing_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.posing_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "purchases":
            summaries = sess.run(
              [model.purchases_err80_summary,
               model.purchases_err160_summary,
               model.purchases_err320_summary,
               model.purchases_err400_summary,
               model.purchases_err560_summary,
               model.purchases_err1000_summary],
              {model.purchases_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.purchases_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.purchases_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.purchases_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.purchases_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.purchases_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "sitting":
            summaries = sess.run(
              [model.sitting_err80_summary,
               model.sitting_err160_summary,
               model.sitting_err320_summary,
               model.sitting_err400_summary,
               model.sitting_err560_summary,
               model.sitting_err1000_summary],
              {model.sitting_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.sitting_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.sitting_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.sitting_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.sitting_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.sitting_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "sittingdown":
            summaries = sess.run(
              [model.sittingdown_err80_summary,
               model.sittingdown_err160_summary,
               model.sittingdown_err320_summary,
               model.sittingdown_err400_summary,
               model.sittingdown_err560_summary,
               model.sittingdown_err1000_summary],
              {model.sittingdown_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.sittingdown_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.sittingdown_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.sittingdown_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.sittingdown_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.sittingdown_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "takingphoto":
            summaries = sess.run(
              [model.takingphoto_err80_summary,
               model.takingphoto_err160_summary,
               model.takingphoto_err320_summary,
               model.takingphoto_err400_summary,
               model.takingphoto_err560_summary,
               model.takingphoto_err1000_summary],
              {model.takingphoto_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.takingphoto_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.takingphoto_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.takingphoto_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.takingphoto_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.takingphoto_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "waiting":
            summaries = sess.run(
              [model.waiting_err80_summary,
               model.waiting_err160_summary,
               model.waiting_err320_summary,
               model.waiting_err400_summary,
               model.waiting_err560_summary,
               model.waiting_err1000_summary],
              {model.waiting_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.waiting_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.waiting_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.waiting_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.waiting_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.waiting_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "walkingdog":
            summaries = sess.run(
              [model.walkingdog_err80_summary,
               model.walkingdog_err160_summary,
               model.walkingdog_err320_summary,
               model.walkingdog_err400_summary,
               model.walkingdog_err560_summary,
               model.walkingdog_err1000_summary],
              {model.walkingdog_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.walkingdog_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.walkingdog_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.walkingdog_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.walkingdog_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.walkingdog_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})
          elif action == "walkingtogether":
            summaries = sess.run(
              [model.walkingtogether_err80_summary,
               model.walkingtogether_err160_summary,
               model.walkingtogether_err320_summary,
               model.walkingtogether_err400_summary,
               model.walkingtogether_err560_summary,
               model.walkingtogether_err1000_summary],
              {model.walkingtogether_err80: mean_mean_errors[1] if FLAGS.seq_length_out >= 2 else None,
               model.walkingtogether_err160: mean_mean_errors[3] if FLAGS.seq_length_out >= 4 else None,
               model.walkingtogether_err320: mean_mean_errors[7] if FLAGS.seq_length_out >= 8 else None,
               model.walkingtogether_err400: mean_mean_errors[9] if FLAGS.seq_length_out >= 10 else None,
               model.walkingtogether_err560: mean_mean_errors[13] if FLAGS.seq_length_out >= 14 else None,
               model.walkingtogether_err1000: mean_mean_errors[24] if FLAGS.seq_length_out >= 25 else None})

          for i in np.arange(len( summaries )):
            model.test_writer.add_summary(summaries[i], current_step)


        print()
        print("============================\n"
              "Learning rate:       %.4f\n"
              "Val mse loss fw:            %.4f\n"
              "srnn mse loss fw:           %.4f\n"
              "============================" % (model.learning_rate.eval(), val_mse_loss_fw, srnn_mse_loss_fw))
        print()


        # Save the model
        if current_step % FLAGS.save_every == 0:
          print( "Saving the model..." )
          model.saver.save(sess, os.path.normpath(os.path.join(train_dir, 'checkpoint')), global_step=current_step )


        sys.stdout.flush()


def get_srnn_gts( actions, model, test_set, data_mean, data_std, dim_to_ignore, one_hot, from_exp=True, to_euler=True ):
  """
  Get the ground truths for srnn's sequences, and convert to Euler angles.
  (the error is always computed in Euler angles).

  Args
    actions: a list of actions to get ground truths for.
    model: training model we are using (we only use the "get_batch" method).
    test_set: dictionary with normalized training data.
    data_mean: d-long vector with the mean of the training data.
    data_std: d-long vector with the standard deviation of the training data.
    dim_to_ignore: dimensions that we are not using to train/predict.
    one_hot: whether the data comes with one-hot encoding indicating action.
    to_euler: whether to convert the angles to Euler format or keep thm in exponential map

  Returns
    srnn_gts_euler: a dictionary where the keys are actions, and the values
      are the ground_truth, denormalized expected outputs of srnns's seeds.
  """
  srnn_gts = {}

  for action in actions:

    srnn_gt = []
    _, _, srnn = model.get_batch_srnn( test_set, action, False )

    for i in np.arange( srnn.shape[0] ):
      denormed = data_utils.unNormalizeData(srnn[i,:,:], data_mean, data_std, dim_to_ignore, actions, one_hot )

      if from_exp and to_euler:
        for j in np.arange( denormed.shape[0] ):
          for k in np.arange(3,97,3):
            denormed[j,k:k+3] = data_utils.rotmat2euler( data_utils.expmap2rotmat( denormed[j,k:k+3] ))

      if not from_exp and not to_euler: # from euler to exp
        for j in np.arange( denormed.shape[0] ):
          for k in np.arange(3,97,3):
            denormed[j,k:k+3] = data_utils.rotmat2expmap( data_utils.euler2rotmat( denormed[j,k:k+3] ))

      srnn_gt.append( denormed )

    # Put back in the dictionary
    srnn_gts[action] = srnn_gt

  return srnn_gts


def sample():
  """Sample predictions for srnn's seeds"""

  if FLAGS.load <= 0:
    raise( ValueError, "Must give an iteration to read parameters from")

  actions = define_actions( FLAGS.action )

  # Use the CPU if asked to
  device_count = {"GPU": 0} if FLAGS.use_cpu else {"GPU": 1}
  with tf.Session(config=tf.ConfigProto( device_count = device_count )) as sess:

    # === Create the model ===
    print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
    sampling     = True
    model = create_model(sess, actions, sampling)
    print("Model created")

    # Load all the data
    train_set, test_set, data_mean, data_std, dim_to_ignore, dim_to_use = read_all_data(
      actions, FLAGS.seq_length_in, FLAGS.seq_length_out, FLAGS.data_dir, not FLAGS.omit_one_hot, FLAGS.train_on_euler )

    # === Read and denormalize the gt with srnn's seeds, as we'll need them many times for evaluation in Euler Angles ===
    srnn_gts_expmap = get_srnn_gts( actions, model, test_set, data_mean,
                              data_std, dim_to_ignore, not FLAGS.omit_one_hot, from_exp=not FLAGS.train_on_euler, to_euler=False )
    srnn_gts_euler = get_srnn_gts( actions, model, test_set, data_mean,
                              data_std, dim_to_ignore, not FLAGS.omit_one_hot, from_exp=not FLAGS.train_on_euler )

    # Clean and create a new h5 file of samples
    SAMPLES_FNAME = 'samples.h5'
    try:
      os.remove( SAMPLES_FNAME )
    except OSError:
      pass

    # Predict and save for each action
    for action in actions:

      # Make prediction with srnn' seeds
      action_prefix, action_postfix_input, action_postfix_output, action_poses = model.get_batch_srnn( test_set, action, FLAGS.velocity )

      forward_only = True
      srnn_seeds = True
      srnn_mse_loss_fw, _, srnn_poses = model.step(sess, action_prefix[:,:,6:], action_postfix_input[:,:,6:], action_postfix_output[:,:,6:], action_poses[:,:,6:], forward_only, srnn_seeds)

      srnn_poses = np.concatenate((np.transpose(action_postfix_output[:, :, :6], [1, 0, 2]), srnn_poses), axis=-1)
      # denorm
      srnn_pred = data_utils.revert_output_format( srnn_poses, action_poses[:,FLAGS.seq_length_in-1,:], data_mean, data_std, dim_to_ignore, actions, not FLAGS.omit_one_hot, FLAGS.velocity )

      # change to expmap
      if FLAGS.train_on_euler:
          for i in np.arange(8):
              for j in np.arange(srnn_pred[i].shape[0]):
                  for k in np.arange(3, 97, 3):
                      srnn_pred[i][j, k:k + 3] = data_utils.rotmat2expmap(
                          data_utils.euler2rotmat(srnn_pred[i][j, k:k + 3]))

      # Save the samples
      with h5py.File( SAMPLES_FNAME, 'a' ) as hf:
        for i in np.arange(8):
          # Save conditioning ground truth
          node_name = 'expmap/gt/{1}_{0}'.format(i, action)
          hf.create_dataset( node_name, data=srnn_gts_expmap[action][i] )
          # Save prediction
          node_name = 'expmap/preds/{1}_{0}'.format(i, action)
          hf.create_dataset( node_name, data=srnn_pred[i] )

      # Compute and save the errors here
      mean_errors = np.zeros( (len(srnn_pred), srnn_pred[0].shape[0]) )

      for i in np.arange(8):

        eulerchannels_pred = srnn_pred[i]
        # change back to euler
        for j in np.arange( eulerchannels_pred.shape[0] ):
          for k in np.arange(3,97,3):
            eulerchannels_pred[j,k:k+3] = data_utils.rotmat2euler(
              data_utils.expmap2rotmat( eulerchannels_pred[j,k:k+3] ))

        eulerchannels_pred[:,0:6] = 0

        # Pick only the dimensions with sufficient standard deviation. Others are ignored.
        idx_to_use = np.where( np.std( eulerchannels_pred, 0 ) > 1e-4 )[0]

        euc_error = np.power( srnn_gts_euler[action][i][:,idx_to_use] - eulerchannels_pred[:,idx_to_use], 2)
        euc_error = np.sum(euc_error, 1)
        euc_error = np.sqrt( euc_error )
        mean_errors[i,:] = euc_error

      mean_mean_errors = np.mean( mean_errors, 0 )

      print("{0: <16} |".format(action), end="")
      for ms in [1, 3, 7, 9, 13, 24]:
          if FLAGS.seq_length_out >= ms + 1:
              print(" {0:.3f} |".format(mean_mean_errors[ms]), end="")
          else:
              print("   n/a |", end="")
      print()

      with h5py.File( SAMPLES_FNAME, 'a' ) as hf:
        node_name = 'mean_{0}_error'.format( action )
        hf.create_dataset( node_name, data=mean_mean_errors )

  return


def define_actions( action ):
  """
  Define the list of actions we are using.

  Args
    action: String with the passed action. Could be "all"
  Returns
    actions: List of strings of actions
  Raises
    ValueError if the action is not included in H3.6M
  """

  actions = ["walking", "eating", "smoking", "discussion",  "directions",
              "greeting", "phoning", "posing", "purchases", "sitting",
              "sittingdown", "takingphoto", "waiting", "walkingdog",
              "walkingtogether"]

  if action in actions:
    return [action]

  if action == "all":
    return actions

  if action == "all_srnn":
    return ["walking", "eating", "smoking", "discussion"]

  raise( ValueError, "Unrecognized action: %d" % action )


def read_all_data( actions, seq_length_in, seq_length_out, data_dir, one_hot, euler ):
  """
  Loads data for training/testing and normalizes it.

  Args
    actions: list of strings (actions) to load
    seq_length_in: number of frames to use in the burn-in sequence
    seq_length_out: number of frames to use in the output sequence
    data_dir: directory to load the data from
    one_hot: whether to use one-hot encoding per action
    euler: whether use euler
  Returns
    train_set: dictionary with normalized training data
    test_set: dictionary with test data
    data_mean: d-long vector with the mean of the training data
    data_std: d-long vector with the standard dev of the training data
    dim_to_ignore: dimensions that are not used becaused stdev is too small
    dim_to_use: dimensions that we are actually using in the model
  """

  # === Read training data ===
  print ("Reading training data (seq_len_in: {0}, seq_len_out {1}).".format(
           seq_length_in, seq_length_out))

  train_subject_ids = [1,6,7,8,9,11]
  test_subject_ids = [5]

  train_set, complete_train = data_utils.load_data( data_dir, train_subject_ids, actions, one_hot, euler )
  test_set,  complete_test  = data_utils.load_data( data_dir, test_subject_ids,  actions, one_hot, euler )

  # Compute normalization stats
  data_mean, data_std, dim_to_ignore, dim_to_use = data_utils.normalization_stats(complete_train)

  # Normalize -- subtract mean, divide by stdev
  train_set = data_utils.normalize_data( train_set, data_mean, data_std, dim_to_use, actions, one_hot )
  test_set  = data_utils.normalize_data( test_set,  data_mean, data_std, dim_to_use, actions, one_hot )
  print("done reading data.")

  return train_set, test_set, data_mean, data_std, dim_to_ignore, dim_to_use


def main(_):
  if FLAGS.sample:
    sample()
  else:
    train()

if __name__ == "__main__":
  tf.app.run()
