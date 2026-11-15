"""Actual supported head layouts and rejected unknown inference evidence."""
import unittest

import numpy as np

from helmet import yolo_post


class HeadContractTests(unittest.TestCase):
    @staticmethod
    def heads(layout='conv'):
        return [np.zeros((1, 21, size, size), np.float32) if layout == 'conv'
                else np.zeros((1, 3, size, size, 7), np.float32) for size in (4, 2, 1)]

    def test_batched_conv_and_legacy_three_finite_empty_heads_are_valid(self):
        for layout in ('conv', 'legacy'):
            with self.subTest(layout=layout):
                heads = self.heads(layout)
                yolo_post.validate_heads(heads, 32, 32)
                boxes, scores, classes = yolo_post.decode(heads, conf_th=.35)
                self.assertEqual(boxes.shape, (0, 4))
                self.assertEqual(scores.shape, (0,))
                self.assertEqual(classes.shape, (0,))

    def test_missing_extra_duplicate_wrong_input_and_nonfinite_heads_are_rejected(self):
        good = self.heads()
        bad = [[], good[:1], good + [good[0]], [good[0], good[0], good[2]],
               [np.zeros((2, 21, 4, 4), np.float32), *good[1:]],
               [np.zeros((1, 18, 4, 4), np.float32), *good[1:]],
               [np.zeros((1, 21, 4, 4), np.complex64), *good[1:]]]
        for value in (np.nan, np.inf):
            head = good[0].copy()
            head[0, 0, 0, 0] = value
            bad.append([head, *good[1:]])
        for heads in bad:
            with self.subTest(shapes=[row.shape for row in heads]):
                with self.assertRaises(ValueError):
                    yolo_post.validate_heads(heads, 32, 32)
                with self.assertRaises(ValueError):
                    yolo_post.decode(heads, conf_th=.35)
        with self.assertRaises(ValueError):
            yolo_post.validate_heads(good, 64, 32)

    def test_rectangular_shuffled_heads_preserve_conv_legacy_decoding(self):
        conv = [np.full((1, 21, 32 // stride, 64 // stride), -20, np.float32)
                for stride in (8, 16, 32)]
        conv[0][0, :7, 1, 1] = [0, 0, 0, 0, 12, 12, -12]
        legacy = [row.reshape(1, 3, 7, *row.shape[2:]).transpose(0, 1, 3, 4, 2) for row in conv]
        yolo_post.validate_heads(conv[::-1], 64, 32)
        yolo_post.validate_heads(legacy[::-1], 64, 32)
        first = yolo_post.decode(conv[::-1], conf_th=.35)
        second = yolo_post.decode(legacy[::-1], conf_th=.35)
        for actual, expected in zip(first, second):
            np.testing.assert_allclose(actual, expected)


if __name__ == '__main__':
    unittest.main()
