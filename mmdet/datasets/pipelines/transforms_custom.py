import inspect
import math
import random
import numbers
import mmcv
import numpy as np
from PIL import Image, ImageOps
from skimage.filters import gaussian
from skimage.restoration import denoise_bilateral
from ..utils.utils import read_image, mask_to_onehot, onehot_to_binary_edges, color2class, inv_mapping

from ..builder import PIPELINES

try:
    from imagecorruptions import corrupt
except ImportError:
    corrupt = None

try:
    import albumentations
    from albumentations import Compose
except ImportError:
    albumentations = None
    Compose = None

try:
    import accimage
except ImportError:
    accimage = None



def _is_pil_image(img):
    if accimage is not None:
        return isinstance(img, (Image.Image, accimage.Image))
    else:
        return isinstance(img, Image.Image)

@PIPELINES.register_module()
class ToPilImage(object):
    def __call__(self, results):
        for key in ['img', 'gt_semantic_seg', 'gt_edge', 'valid_pixels']:
            if key in results:
                if not _is_pil_image(results[key]):
                    results[key] = Image.fromarray(results[key])
        return results

@PIPELINES.register_module
class ReadImage(object):
    def __init__(self, get_edge=False):
        self.get_edge = get_edge

    def __call__(self, results, binarize_label=False):
        assert 'img_path' in results
        img_path = results['img_path']
        img = read_image(img_path, 'image', results['n_channels']) if 'n_channels' in results else read_image(
            img_path, 'image', 3)
        if 'label_path' in results and results['label_path'] is not None:
            label_path = results['label_path']
            assert label_path.split('.')[-1] in ['tif', 'png', 'jpg', 'jpeg', 'ppm', 'bmp']
            assert 'rgb2label' in results, 'Mapping from RGB to label must be provided.'
            label_img_rgb = read_image(label_path, 'label')
            if binarize_label:
                label_img_rgb[label_img_rgb < 128] = 0
                label_img_rgb[label_img_rgb >= 128] = 255
            label_img = color2class(label_img_rgb, results['rgb2label']).astype(np.uint8)
        else:
            label_img = np.zeros(img.shape[:2], dtype=np.uint8)

        if 'tile_size' in results and 'h' in results and 'w' in results:
            i = results['h']
            j = results['w']
            tile_size = results['tile_size']
            img = img[i:i + tile_size, j:j + tile_size, :]
            label_img = label_img[i:i + tile_size, j:j + tile_size]

        results['img'] = img
        results['gt_semantic_seg'] = label_img

        if self.get_edge:
            assert 'lable_path' in results and 'num_classes' in results
            if label_img.any():
                edgemap = mask_to_onehot(label_img, results['num_classes'])
                edgemap = onehot_to_binary_edges(edgemap, 2, results['num_classes'])
            else:
                edgemap = np.zeros((label_img.shape), dtype=np.uint8)
            results['gt_edge'] = edgemap

        return results


@PIPELINES.register_module
class ToArray(object):
    def __init__(self, normalize=False, mean=None, std=None):
        self.normalize = normalize
        self.mean = np.array(mean, dtype=np.float32) if mean is not None else mean
        self.std = np.array(std, dtype=np.float32) if std is not None else std

    def __call__(self, results):
        if self.normalize:
            results['img'] = ((np.array(results['img'], dtype=np.float32) - self.mean) / self.std).transpose((2, 0, 1))
        else:
            results['img'] = (np.array(results['img'], dtype=np.float32) - 128).transpose((2, 0, 1))
        results['gt_semantic_seg'] = np.array(results['gt_semantic_seg'], dtype=np.uint8)
        if 'gt_edge' in results:
            results['gt_edge'] = np.array(results['gt_edge'], dtype=np.float32)
        if 'valid_pixels' in results:
            results['valid_pixels'] = np.array(results['valid_pixels'], dtype=np.float32)
        results['img_norm_cfg'] = dict(normalize=self.normalize, mean=self.mean, std=self.std)
        return results


@PIPELINES.register_module
class Resize(object):
    '''
    Resize image to exact size of crop
    '''
    def __init__(self, size):
        self.size = (size, size)

    def __call__(self, results):
        results['img'] = results['img'].resize(self.size, Image.BICUBIC)
        for key in ['gt_semantic_seg', 'gt_edge', 'valid_pixels']:
            if key in results:
                results[key] = results[key].resize(self.size, Image.NEAREST)
        results['img_shape'] = self.size
        return results


@PIPELINES.register_module()
class ResizeImg(object):
    '''
    Resize image to exact size of crop
    '''
    def __init__(self, size):
        self.size = (size, size)

    def __call__(self, results):
        results['img'] = results['img'].resize(self.size, Image.BICUBIC)
        results['img_shape'] = self.size
        return results


@PIPELINES.register_module()
class CenterCrop(object):
    def __init__(self, size):
        if isinstance(size, numbers.Number):
            self.size = (int(size), int(size))
        else:
            self.size = size

    def __call__(self, results):

        w, h = results['img'].size
        th, tw = self.size
        x1 = int(round((w - tw) / 2.0))
        y1 = int(round((h - th) / 2.0))

        results['img'] = results['img'].crop((x1, y1, x1 + tw, y1 + th))
        for key in ['gt_semantic_seg', 'gt_edge', 'valid_pixels']:
            if key in results:
                results[key] = results[key].crop((x1, y1, x1 + tw, y1 + th))
            results['img_shape'] = self.size

        return results


@PIPELINES.register_module()
class RandomCrop(object):
    '''
    Take a random crop from the image.

    First the image or crop size may need to be adjusted if the incoming image
    is too small...

    If the image is smaller than the crop, then:
         the image is padded up to the size of the crop
         unless 'nopad', in which case the crop size is shrunk to fit the image

    A random crop is taken such that the crop fits within the image.
    If a centroid is passed in, the crop must intersect the centroid.
    '''
    def __init__(self, size, ignore_index=0, nopad=True):
        if isinstance(size, numbers.Number):
            self.size = (int(size), int(size))
        else:
            self.size = size
        self.ignore_index = ignore_index
        self.nopad = nopad
        self.pad_color = (0, 0, 0)

    def __call__(self, results, centroid=None):
        w, h = results['img'].size
        # ASSUME H, W
        th, tw = self.size
        if w != tw or h != th:
            if self.nopad:
                if th > h or tw > w:
                    # Instead of padding, adjust crop size to the shorter edge of image.
                    shorter_side = min(w, h)
                    th, tw = shorter_side, shorter_side
            else:
                # Check if we need to pad img to fit for crop_size.
                if th > h:
                    pad_h = (th - h) // 2 + 1
                else:
                    pad_h = 0
                if tw > w:
                    pad_w = (tw - w) // 2 + 1
                else:
                    pad_w = 0
                border = (pad_w, pad_h, pad_w, pad_h)
                if pad_h or pad_w:
                    if 'img' in results:
                        results['img'] = ImageOps.expand(results['img'], border=border, fill=self.pad_color)
                    if 'gt_semantic_seg' in results:
                        results['gt_semantic_seg'] = ImageOps.expand(results['gt_semantic_seg'], border=border, fill=self.ignore_index)
                    if 'gt_edge' in results:
                        results['gt_edge'] = ImageOps.expand(results['gt_edge'], border=border, fill=0)
                    if 'valid_pixels' in results:
                        results['valid_pixels'] = ImageOps.expand(results['valid_pixels'], border=border, fill=0)
                
                    w, h = results['img'].size
            if centroid is not None:
                # Need to insure that centroid is covered by crop and that crop
                # sits fully within the image
                c_x, c_y = centroid
                max_x = w - tw
                max_y = h - th
                x1 = random.randint(c_x - tw, c_x)
                x1 = min(max_x, max(0, x1))
                y1 = random.randint(c_y - th, c_y)
                y1 = min(max_y, max(0, y1))
            else:
                if w == tw:
                    x1 = 0
                else:
                    x1 = random.randint(0, w - tw)
                if h == th:
                    y1 = 0
                else:
                    y1 = random.randint(0, h - th)
            results['img'] = results['img'].crop((x1, y1, x1 + tw, y1 + th))
            for key in ['gt_semantic_seg', 'gt_edge', 'valid_pixels']:
                if key in results:
                    results[key] = results[key].crop((x1, y1, x1 + tw, y1 + th))
            results['img_shape'] = self.size

        return results



@PIPELINES.register_module()
class RandomSizeAndCrop(object):
    def __init__(self, size, crop_nopad=True, scale_min=0.5, scale_max=2.0, ignore_index=0, pre_size=None):
        self.size = size
        self.crop = RandomCrop(self.size, ignore_index=ignore_index, nopad=crop_nopad)
        self.scale_min = scale_min
        self.scale_max = scale_max
        self.pre_size = pre_size

    def __call__(self, results, centroid=None):
        #assert results['img'].size == results['gt_semantic_seg'].size == results['gt_edge'].size
        img = results['img']

        # first, resize such that shorter edge is pre_size
        if self.pre_size is None:
            scale_amt = 1.
        elif img.size[1] < img.size[0]:
            scale_amt = self.pre_size / img.size[1]
        else:
            scale_amt = self.pre_size / img.size[0]
        scale_amt *= random.uniform(self.scale_min, self.scale_max)
        w, h = [int(i * scale_amt) for i in img.size]

        if centroid is not None:
            centroid = [int(c * scale_amt) for c in centroid]

        results['img'] = results['img'].resize((w, h), Image.BICUBIC)
        for key in ['gt_semantic_seg', 'gt_edge', 'valid_pixels']:
            if key in results:
                results[key] = results[key].resize((w, h), Image.NEAREST)

        return self.crop(results, centroid)


@PIPELINES.register_module()
class PadImage(object):
    def __init__(self, size, ignore_index):
        self.size = size
        self.ignore_index = ignore_index

    def __call__(self, results):
        #assert results['img'].size == results['gt_semantic_seg'].size == results['gt_edge'].size
        th, tw = self.size, self.size
        img = results['img']
        w, h = img.size
        
        if w > tw or h > th :
            wpercent = (tw/float(w))    
            target_h = int((float(img.size[1])*float(wpercent)))
            results['img'] = results['img'].resize((tw, target_h), Image.BICUBIC)
            for key in ['gt_semantic_seg', 'gt_edge', 'valid_pixels']:
                if key in results:
                    results[key] = results[key].resize((tw, target_h), Image.NEAREST)

        w, h = results['img'].size
        ##Pad
        results['img'] = ImageOps.expand(results['img'], border=(0,0,tw-w, th-h), fill=0)
        results['gt_semantic_seg'] = ImageOps.expand(results['gt_semantic_seg'], border=(0,0,tw-w, th-h), fill=self.ignore_index)
        for key in ['gt_edge', 'valid_pixels']:
            if key in results:
                results[key] = ImageOps.expand(results[key], border=(0, 0, tw - w, th - h), fill=0)
        results['img_shape'] = (th, tw)
        
        return results


@PIPELINES.register_module()
class HorizontallyFlip(object):
    def __call__(self, results):
        for key in ['img', 'gt_semantic_seg', 'gt_edge', 'valid_pixels']:
            if key in results:
                results[key] = results[key].transpose(Image.FLIP_LEFT_RIGHT)
        results['horizontal_flip'] = not results['horizontal_flip'] if 'horizontal_flip' in results else True

        return results

@PIPELINES.register_module()
class RandomHorizontallyFlip(object):
    def __init__(self, p=0.5):
        self.p = p
    def __call__(self, results):
        if random.random() < self.p:
            flip = HorizontallyFlip()
            results = flip(results)
        else:
            results['horizontal_flip'] = results['horizontal_flip'] if 'horizontal_flip' in results else False
        return results


@PIPELINES.register_module()
class VerticalFlip(object):
    def __call__(self, results):
        for key in ['img', 'gt_semantic_seg', 'gt_edge', 'valid_pixels']:
            if key in results:
                results[key] = results[key].transpose(Image.FLIP_TOP_BOTTOM)
        results['vertical_flip'] = not results['vertical_flip'] if 'vertical_flip' in results else True
        return results


@PIPELINES.register_module()
class RandomVerticalFlip(object):
    def __init__(self, p=0.5):
        self.p = p
    def __call__(self, results):
        if random.random() < self.p:
            flip = VerticalFlip()
            results = flip(results)
        else:
            results['vertical_flip'] = results['vertical_flip'] if 'vertical_flip' in results else False
        return results


@PIPELINES.register_module()
class Rotate(object):
    def __init__(self, degree):
        assert degree in [90, 180, 270]
        self.degree = degree

    def __call__(self, results):
        results['img'] = results['img'].rotate(self.degree, Image.BICUBIC)
        for key in ['gt_semantic_seg', 'gt_edge', 'valid_pixels']:
            if key in results:
                results[key] = results[key].rotate(self.degree, Image.NEAREST)
        results['rotate'] = (self.degree + results['rotate']) if 'rotate' in results else self.degree
        return results


@PIPELINES.register_module()
class RandomRotate(object):
    def __init__(self, degree=90):
        assert degree in [90, 180, 270]
        self.degree = degree

    def __call__(self, results):
        if random.random() < 0.5:
            rotate = Rotate(degree=self.degree)
            results = rotate(results)
        else:
            results['rotate'] = results['rotate'] if 'rotate' in results else 0
        return results


@PIPELINES.register_module()
class RandomGaussianBlur(object):
    def __init__(self, p=0.5):
        self.p = p

    def __call__(self, results):
        if random.random() < self.p:
            sigma = 0.15 + random.random() * 1.15
            blurred_img = gaussian(np.array(results['img']), sigma=sigma, multichannel=True)
            blurred_img *= 255
            results['img'] = Image.fromarray(blurred_img.astype(np.uint8))
        return results


@PIPELINES.register_module
class RandomBilateralBlur(object):
    def __init__(self, p=0.5):
        self.p = p

    def __call__(self, results):
        if random.random() < self.p:
            sigma = random.uniform(0.05,0.75)
            blurred_img = denoise_bilateral(np.array(results['img']), sigma_spatial=sigma, multichannel=True)
            blurred_img *= 255
            results['img'] = Image.fromarray(blurred_img.astype(np.uint8))
        return results