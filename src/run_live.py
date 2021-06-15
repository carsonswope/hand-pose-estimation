import pyrealsense2 as rs
import numpy as np
import cv2

import pycuda.driver as cu
import pycuda.autoinit
import pycuda.curandom as cu_rand

from decision_tree import *
from cuda.points_ops import *
from calibrated_plane import *
np.set_printoptions(suppress=True)

import argparse

def main():

    parser = argparse.ArgumentParser(description='Train a classifier RDF for depth images')
    parser.add_argument('-m', '--model', nargs='?', required=True, type=str, help='Path to .npy model input file')
    parser.add_argument('-d', '--data', nargs='?', required=True, type=str, help='Directory holding data')
    parser.add_argument('--rs_bag', nargs='?', required=False, type=str, help='Path to optional input realsense .bag file to use instead of live camera stream')
    parser.add_argument('--plane_num_iterations', nargs='?', required=False, type=int, help='Num random planes to propose looking for best fit')
    parser.add_argument('--plane_z_threshold', nargs='?', required=True, type=float, help='Z-value threshold in plane coordinates for clipping depth image pixels')
    args = parser.parse_args()

    MODEL_OUT_NAME = args.model
    DATASET_PATH = args.data
    RS_BAG = args.rs_bag

    NUM_RANDOM_GUESSES = args.plane_num_iterations or 25000
    PLANE_Z_OUTLIER_THRESHOLD = args.plane_z_threshold

    calibrated_plane = CalibratedPlane(NUM_RANDOM_GUESSES, PLANE_Z_OUTLIER_THRESHOLD)

    print('loading forest')
    forest = DecisionForest.load(MODEL_OUT_NAME)
    data_config = DecisionTreeDatasetConfig(DATASET_PATH)

    print('compiling CUDA kernels..')
    decision_tree_evaluator = DecisionTreeEvaluator()
    points_ops = PointsOps()

    print('initializing camera..')
    # Configure depth and color streams
    pipeline = rs.pipeline()
    config = rs.config()

    if RS_BAG:
        config.enable_device_from_file(RS_BAG, repeat_playback=True)
        config.enable_stream(rs.stream.depth, rs.format.z16)
        config.enable_stream(rs.stream.color, rs.format.rgb8)

    else:
        # Get device product line for setting a supporting resolution
        pipeline_wrapper = rs.pipeline_wrapper(pipeline)
        pipeline_profile = config.resolve(pipeline_wrapper)
        device = pipeline_profile.get_device()
        device_config_json = open('hand_config.json', 'r').read()
        rs.rs400_advanced_mode(device).load_json(device_config_json)
        device.first_depth_sensor().set_option(rs.option.depth_units, 0.0001)
        config.enable_stream(rs.stream.depth, 848, 480, rs.format.z16, 90)

    profile = pipeline.start(config)
    if RS_BAG:
        profile.get_device().as_playback().set_real_time(False)
    depth_profile = profile.get_stream(rs.stream.depth).as_video_stream_profile()
    depth_intrin = depth_profile.get_intrinsics()

    DIM_X = depth_intrin.width
    DIM_Y = depth_intrin.height

    FOCAL = depth_intrin.fx
    PP = np.array([depth_intrin.ppx, depth_intrin.ppy], dtype=np.float32)
    pts_cu = cu_array.GPUArray((DIM_Y, DIM_X, 4), dtype=np.float32)
    depth_image_cu = cu_array.GPUArray((1, DIM_Y, DIM_X), dtype=np.uint16)
    labels_image_cu = cu_array.GPUArray((1, DIM_Y, DIM_X), dtype=np.uint16)

    try:

        frame_num = 0

        while True:

            # Wait for a coherent pair of frames: depth and color
            frames = pipeline.wait_for_frames()
            depth_frame = frames.get_depth_frame()
            if not depth_frame:
                continue

            # let camera stabilize for a few frames
            if frame_num < 15:
                frame_num += 1
                continue

            # Convert images to numpy arrays
            depth_image = np.asanyarray(depth_frame.get_data())
            depth_image_cu.set(depth_image)

            grid_dim = (1, (DIM_X // 32) + 1, (DIM_Y // 32) + 1)
            block_dim = (1,32,32)

            # convert depth image to points
            points_ops.deproject_points(
                np.array([1, DIM_X, DIM_Y, -1], dtype=np.int32),
                PP,
                np.float32(FOCAL),
                depth_image_cu,
                pts_cu,
                grid=grid_dim,
                block=block_dim)

            if not calibrated_plane.is_set():
                calibrated_plane.make(pts_cu, (DIM_X, DIM_Y))

            # every point..
            grid_dim2 = (((DIM_X * DIM_Y) // 1024) + 1, 1, 1)
            block_dim2 = (1024, 1, 1)

            points_ops.transform_points(
                np.int32(DIM_X * DIM_Y),
                pts_cu,
                calibrated_plane.get_mat(),
                grid=grid_dim2,
                block=block_dim2)

            calibrated_plane.filter_points_by_plane(
                np.int32(DIM_X * DIM_Y),
                np.float32(PLANE_Z_OUTLIER_THRESHOLD),
                pts_cu,
                grid=grid_dim2,
                block=block_dim2)

            points_ops.setup_depth_image_for_forest(
                np.int32(DIM_X * DIM_Y),
                pts_cu,
                depth_image_cu,
                grid=grid_dim2,
                block=block_dim2)

            labels_image_cu.fill(np.uint16(65535))
            decision_tree_evaluator.get_labels_forest(forest, depth_image_cu, labels_image_cu)

            # final steps: these are slow.
            # can be polished if/when necessary
            labels_image_cpu = labels_image_cu.get()
            labels_image_cpu_rgba = data_config.convert_ids_to_colors(labels_image_cpu).reshape((480, 848, 4))

            labels_image_cpu_bgra = cv2.cvtColor(labels_image_cpu_rgba, cv2.COLOR_RGB2BGR)

            cv2.namedWindow('RealSense', cv2.WINDOW_AUTOSIZE)
            cv2.imshow('RealSense', labels_image_cpu_bgra)
            
            cv2.waitKey(1)

            frame_num += 1


    finally:

        # Stop streaming
        pipeline.stop()

if __name__ == '__main__':
    main()
