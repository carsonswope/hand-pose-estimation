import numpy as np
from numpy.core.shape_base import block
import cuda.py_nvcc_utils as py_nvcc_utils


# uses mean-shift to find 'mode' of group of 2d points
# https://en.wikipedia.org/wiki/Mean_shift

class MeanShift:
    def __init__(self):
        cu_mod = py_nvcc_utils.get_module('src/cuda/mean_shift.cu')
        self._make_composite_labels_image = cu_mod.get_function('make_composite_labels_image')

    def make_composite_labels_image(self, images, dim_x, dim_y, labels_decision_tree, composite_image):

        # every point..
        grid_dim = ((dim_x // 32) + 1, (dim_y // 32) + 1, 1)
        block_dim = (32, 32, 1)

        self._make_composite_labels_image(
            images,
            np.int32(images.shape[0]),
            np.int32(dim_x),
            np.int32(dim_y),
            labels_decision_tree,
            composite_image,
            grid=grid_dim,
            block=block_dim)
    
    def run(self, match, variance):

        x = np.where(match)
        x = np.array([x[0], x[1]]).T

        start_mean = np.sum(x, axis=0) / x.shape[0]
        mean = np.copy(start_mean)

        ROUNDS = 10

        for _ in range(ROUNDS):
            diff = x - mean
            dist_sq = np.sum(np.power(diff, 2), axis=1)
            e_pow = -dist_sq / (2 * variance * variance)
            f = np.power(np.e, e_pow)
            m = np.sum(f.reshape((f.shape[0], 1)) * diff, axis=0) / np.sum(f) 
            mean += m

        return mean
