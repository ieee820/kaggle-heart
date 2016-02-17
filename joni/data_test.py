import cPickle as pickle
import glob
import re
from collections import namedtuple
import numpy as np
import skimage.exposure
import skimage.io
import skimage.restoration
import skimage.transform
from scipy.fftpack import fftn, ifftn
from skimage.draw import ellipse
from skimage.feature import peak_local_max, canny
from skimage.transform import hough_circle
from skimage.util import img_as_ubyte

rng = np.random.RandomState(42)


def read_labels(file_path):
    id2labels = {}
    train_csv = open(file_path)
    lines = train_csv.readlines()
    i = 0
    for item in lines:
        if i == 0:
            i = 1
            continue
        id, systole, diastole = item.replace('\n', '').split(',')
        id2labels[int(id)] = [float(systole), float(diastole)]
    return id2labels


def read_patients_data(paths):
    data_dict = {}
    for p in paths:
        patient_id = int(re.search(r'/(\d+)/', p).group(1))
        data_dict[patient_id] = read_patient(p)
    return data_dict


def read_patient(path):
    slices_paths = sorted(glob.glob(path + '/*.pkl'))
    slices_dict = {}
    for sp in slices_paths:
        slice = re.search(r'/(\w+\d+)*\.pkl$', sp).group(1)
        slices_dict[slice] = read_slice(sp)
    return slices_dict


def read_slice(path):
    return pickle.load(open(path))['data']


def read_metadata(path):
    d = pickle.load(open(path))['metadata'][0]
    metadata = {k: d[k] for k in ['PixelSpacing', 'ImageOrientationPatient', 'SliceLocation',
                                  'PatientSex', 'PatientAge']}
    metadata['PixelSpacing'] = np.float32(metadata['PixelSpacing'])
    metadata['ImageOrientationPatient'] = np.float32(metadata['ImageOrientationPatient'])
    metadata['SliceLocation'] = np.float32(metadata['SliceLocation'])
    metadata['PatientSex'] = 1 if metadata['PatientSex'] == 'F' else 0
    metadata['PatientAge'] = int(metadata['PatientAge'][1:3])
    return metadata


def sample_augmentation_parameters(transformation):
    random_params = None
    if all([transformation['rotation_range'],
            transformation['translation_range'],
            transformation['shear_range'],
            transformation['do_flip'],
            transformation['sequence_shift']]):
        shift_x = rng.uniform(*transformation['translation_range'])
        shift_y = rng.uniform(*transformation['translation_range'])
        translation = (shift_x, shift_y)
        rotation = rng.uniform(*transformation['rotation_range'])
        shear = rng.uniform(*transformation['shear_range'])
        flip = rng.randint(2) > 0 if transformation['do_flip'] else False  # flip half of the time
        sequence_shift = rng.randint(30) if transformation['sequence_shift']  else 0
        random_params = namedtuple('Params', ['translation', 'rotation', 'shear', 'flip', 'sequence_shift'])(
            translation,
            rotation,
            shear, flip,
            sequence_shift)
    return random_params


def transform_norm_rescale(data, metadata, transformation, random_augmentation_params=None, shift_center=(.4, .5),
                           normalised_patch_size=(200, 200)):
    """
    :param data: (30, height, width) matrix from one slice of MRI
    :param transformation:
    :return:
    """
    patch_size = transformation['patch_size']
    out_shape = (30,) + patch_size
    out_data = np.zeros(out_shape, dtype='float32')

    # correct orientation
    data = correct_orientation(data, metadata)

    # if random_augmentation_params=None -> sample new params
    # if the transformation implies no augmentations then random_augmentation_params remains None
    if not random_augmentation_params:
        random_augmentation_params = sample_augmentation_parameters(transformation)

    # build scaling transformation
    pixel_spacing = metadata['PixelSpacing']
    assert pixel_spacing[0] == pixel_spacing[1]
    current_shape = data.shape[-2:]
    normalised_shape = tuple(int(float(d) * ps) for d, ps in zip(current_shape, pixel_spacing))
    # scale the images such that they all have the same scale
    norm_rescaling = 1. / pixel_spacing[0]
    tform_normscale = build_rescale_transform(norm_rescaling, data.shape[-2:], target_shape=normalised_shape)
    tform_shift_center, tform_shift_uncenter = build_shift_center_transform(normalised_shape, shift_center,
                                                                            normalised_patch_size)

    patch_scale = max(1. * normalised_patch_size[0] / patch_size[0], 1. * normalised_patch_size[1] / patch_size[1])
    tform_patch_scale = build_rescale_transform(patch_scale, normalised_patch_size, target_shape=patch_size)

    total_tform = tform_patch_scale + tform_shift_uncenter + tform_shift_center + tform_normscale

    # build random augmentation
    if random_augmentation_params:
        augment_tform = build_augmentation_transform(rotation=random_augmentation_params.rotation,
                                                     shear=random_augmentation_params.shear,
                                                     translation=random_augmentation_params.translation,
                                                     flip=random_augmentation_params.flip)
        total_tform = tform_patch_scale + tform_shift_uncenter + augment_tform + tform_shift_center + tform_normscale

    # apply transformation per image
    for i in xrange(data.shape[0]):
        out_data[i] = fast_warp(data[i], total_tform, output_shape=patch_size)

    normalize_contrast_zmuv(out_data)

    # if the sequence is < 30 timesteps, copy last image
    if data.shape[0] < out_shape[0]:
        for j in xrange(data.shape[0], out_shape[0]):
            out_data[j] = out_data[-1]

    # if > 30, remove images
    if data.shape[0] > out_shape[0]:
        out_data = out_data[:30]

    # shift the sequence for a number of time steps
    if random_augmentation_params:
        out_data = np.roll(out_data, random_augmentation_params.sequence_shift, axis=0)

    return out_data


tform_identity = skimage.transform.AffineTransform()


def fast_warp(img, tf, output_shape, mode='constant', order=1):
    """
    This wrapper function is faster than skimage.transform.warp
    """
    m = tf.params  # tf._matrix is
    return skimage.transform._warps_cy._warp_fast(img, m, output_shape=output_shape, mode=mode, order=order)


def build_centering_transform(image_shape, target_shape=(50, 50)):
    rows, cols = image_shape
    trows, tcols = target_shape
    shift_x = (cols - tcols) / 2.0
    shift_y = (rows - trows) / 2.0
    return skimage.transform.SimilarityTransform(translation=(shift_x, shift_y))


def build_rescale_transform(downscale_factor, image_shape, target_shape):
    """
    estimating the correct rescaling transform is slow, so just use the
    downscale_factor to define a transform directly. This probably isn't
    100% correct, but it shouldn't matter much in practice.
    """
    rows, cols = image_shape
    trows, tcols = target_shape
    tform_ds = skimage.transform.AffineTransform(scale=(downscale_factor, downscale_factor))

    # centering
    shift_x = cols / (2.0 * downscale_factor) - tcols / 2.0
    shift_y = rows / (2.0 * downscale_factor) - trows / 2.0
    tform_shift_ds = skimage.transform.SimilarityTransform(translation=(shift_x, shift_y))
    return tform_shift_ds + tform_ds


def build_center_uncenter_transforms(image_shape):
    """
    These are used to ensure that zooming and rotation happens around the center of the image.
    Use these transforms to center and uncenter the image around such a transform.
    """
    center_shift = np.array(
        [image_shape[1], image_shape[0]]) / 2.0 - 0.5  # need to swap rows and cols here apparently! confusing!
    tform_uncenter = skimage.transform.SimilarityTransform(translation=-center_shift)
    tform_center = skimage.transform.SimilarityTransform(translation=center_shift)
    return tform_center, tform_uncenter


def build_augmentation_transform(rotation=0, shear=0, translation=(0, 0), flip=False, zoom=(1.0, 1.0)):
    if flip:
        shear += 180
        # shear by 180 degrees is equivalent to flip along the X-axis

    tform_augment = skimage.transform.AffineTransform(scale=(1 / zoom[0], 1 / zoom[1]), rotation=np.deg2rad(rotation),
                                                      shear=np.deg2rad(shear), translation=translation)
    return tform_augment


def build_orientation_correction_transform(metadata):
    tform_total = tform_identity

    F = np.array(metadata["ImageOrientationPatient"]).reshape((2, 3))
    fy = F[1, :]
    fx = F[0, :]
    # unit vectors of patient coordinates
    y_e = np.array([0, 1, 0])

    if abs(np.dot(y_e, fy)) >= abs(np.dot(y_e, fx)):
        print 'T'
        tform_total += skimage.transform.AffineTransform(shear=np.deg2rad(180))
        tform_total += skimage.transform.AffineTransform(rotation=np.deg2rad(90))

    return tform_total


def build_shift_center_transform(image_shape, center_location, patch_size):
    """Shifts the center of the image to a given location.
    This function tries to include as much as possible of the image in the patch
    centered around the new center. If the patch arount the ideal center
    location doesn't fit within the image, we shift the center to the right so
    that it does.
    """
    center_absolute_location = [
        center_location[0] * image_shape[1], center_location[1] * image_shape[0]]

    # Check for overlap at the edges
    center_absolute_location[0] = max(
        center_absolute_location[0], patch_size[1] / 2.0)
    center_absolute_location[1] = max(
        center_absolute_location[1], patch_size[0] / 2.0)
    center_absolute_location[0] = min(
        center_absolute_location[0], image_shape[1] - patch_size[1] / 2.0)
    center_absolute_location[1] = min(
        center_absolute_location[1], image_shape[0] - patch_size[0] / 2.0)

    # Check for overlap at both edges
    if patch_size[0] > image_shape[0]:
        center_absolute_location[1] = image_shape[0] / 2.0
    if patch_size[1] > image_shape[1]:
        center_absolute_location[0] = image_shape[1] / 2.0

    # Build transform
    new_center = np.array(center_absolute_location)
    translation_center = new_center - 0.5
    translation_uncenter = -np.array((patch_size[1] / 2.0, patch_size[0] / 2.0)) - 0.5
    return (
        skimage.transform.SimilarityTransform(translation=translation_center),
        skimage.transform.SimilarityTransform(translation=translation_uncenter))


# def normalize_contrast_zmuv(data, z=2):
#     mean = np.mean(data)
#     std = np.std(data)
#     for i in xrange(len(data)):
#         img = data[i]
#         img = ((img - mean) / (2 * std * z) + 0.5)
#         data[i] = np.clip(img, -0.0, 1.0)

def normalize_contrast_zmuv(imdata, metadata=None, z=2):
    flat_data = np.concatenate([i.flatten() for i in imdata]).flatten()
    mean = np.mean(flat_data)
    std = np.std(flat_data)
    for i in xrange(len(imdata)):
        image = imdata[i]
        image = ((image - mean) / (2 * std * z) + 0.5)
        image = np.clip(image, -0.0, 1.0)
        imdata[i] = image

    return imdata


def correct_orientation(data, metadata):
    F = metadata["ImageOrientationPatient"].reshape((2, 3))
    f_1 = F[1, :]
    f_2 = F[0, :]
    y_e = np.array([0, 1, 0])
    if abs(np.dot(y_e, f_1)) >= abs(np.dot(y_e, f_2)):
        out_data = np.transpose(data, (0, 2, 1))
    else:
        out_data = data
    return out_data


def extract_roi_joni(data, kernel_width=5, center_margin=8, num_peaks=10, num_circles=20, upscale=1.5, minradius=20,
                     maxradius=60, radstep=2):
    ximagesize = data[0]['data'].shape[1]
    yimagesize = data[0]['data'].shape[2]

    xsurface = np.tile(range(ximagesize), (yimagesize, 1)).T
    ysurface = np.tile(range(yimagesize), (ximagesize, 1))
    lsurface = np.zeros((ximagesize, yimagesize))

    allcenters = []
    allaccums = []
    allradii = []

    for ddi in data:
        outdata = ddi['data']
        ff1 = fftn(outdata)
        fh = np.absolute(ifftn(ff1[1, :, :]))
        fh[fh < 0.1 * np.max(fh)] = 0.0
        # image = img_as_ubyte(fh / np.max(fh))
        image = fh / np.max(fh)

        # find hough circles
        edges = canny(image, sigma=3)  # , low_threshold=10, high_threshold=50)

        # Detect two radii
        hough_radii = np.arange(minradius, maxradius, radstep)
        hough_res = hough_circle(edges, hough_radii)

        centers = []
        accums = []
        radii = []

        for radius, h in zip(hough_radii, hough_res):
            # For each radius, extract num_peaks circles
            peaks = peak_local_max(h, num_peaks=num_peaks)
            centers.extend(peaks)
            accums.extend(h[peaks[:, 0], peaks[:, 1]])
            radii.extend([radius] * num_peaks)

        # Keep the most prominent num_circles circles
        sorted = np.argsort(accums)[::-1][:num_circles]

        for idx in sorted:
            center_x, center_y = centers[idx]
            allcenters.append(centers[idx])
            allradii.append(radii[idx])
            allaccums.append(accums[idx])
            brightness = accums[idx]
            lsurface = lsurface + brightness * np.exp(
                -((xsurface - center_x) ** 2 + (ysurface - center_y) ** 2) / kernel_width ** 2)

    lsurface = lsurface / lsurface.max()

    # select most likely ROI center
    x_axis, y_axis = np.unravel_index(lsurface.argmax(), lsurface.shape)

    # determine ROI radius
    x_radius = 0
    y_radius = 0
    for idx in range(len(allcenters)):
        xshift = np.abs(allcenters[idx][0] - x_axis)
        yshift = np.abs(allcenters[idx][1] - y_axis)
        if (xshift <= center_margin) & (yshift <= center_margin):
            x_radius = np.max((x_radius, allradii[idx] + xshift))
            y_radius = np.max((y_radius, allradii[idx] + yshift))

    # print x_axis, y_axis, x_radius, y_radius
    # print allcenters

    x_radius = upscale * x_radius
    y_radius = upscale * y_radius

    ROImask = np.zeros_like(lsurface)
    [rr, cc] = ellipse(x_axis, y_axis, x_radius, y_radius)
    ROImask[rr, cc] = 1.
    # plt.figure()
    # plt.imshow(ROImask, cmap='gist_gray_r', vmin=0., vmax=1.)
    # plt.show()

    print (x_axis, y_axis), x_radius, y_radius
    return lsurface, ROImask, (x_axis, y_axis)


def extract_roi(data, minradius, maxradius, kernel_width=5, center_margin=8, num_peaks=10,
                num_circles=20, upscale=1.5, radstep=2):
    ximagesize = data[0]['data'].shape[1]
    yimagesize = data[0]['data'].shape[2]

    xsurface = np.tile(range(ximagesize), (yimagesize, 1)).T
    ysurface = np.tile(range(yimagesize), (ximagesize, 1))
    lsurface = np.zeros((ximagesize, yimagesize))

    allcenters = []
    allaccums = []
    allradii = []

    for ddi in data:
        outdata = ddi['data']
        ff1 = fftn(outdata)
        fh = np.absolute(ifftn(ff1[1, :, :]))
        fh[fh < 0.1 * np.max(fh)] = 0.0
        image = fh / np.max(fh)

        # find hough circles and detect two radii
        edges = canny(image, sigma=3)
        hough_radii = np.arange(minradius, maxradius, radstep)
        hough_res = hough_circle(edges, hough_radii)

        centers = []
        accums = []
        radii = []

        for radius, h in zip(hough_radii, hough_res):
            # For each radius, extract num_peaks circles
            peaks = peak_local_max(h, num_peaks=num_peaks)
            centers.extend(peaks)
            accums.extend(h[peaks[:, 0], peaks[:, 1]])
            radii.extend([radius] * num_peaks)

        # Keep the most prominent num_circles circles
        sorted_circles_idxs = np.argsort(accums)[::-1][:num_circles]

        for idx in sorted_circles_idxs:
            center_x, center_y = centers[idx]
            allcenters.append(centers[idx])
            allradii.append(radii[idx])
            allaccums.append(accums[idx])
            brightness = accums[idx]
            lsurface = lsurface + brightness * np.exp(
                -((xsurface - center_x) ** 2 + (ysurface - center_y) ** 2) / kernel_width ** 2)

    lsurface = lsurface / lsurface.max()

    # select most likely ROI center
    x_axis, y_axis = np.unravel_index(lsurface.argmax(), lsurface.shape)

    # determine ROI radius
    x_radius = 0
    y_radius = 0
    for idx in range(len(allcenters)):
        xshift = np.abs(allcenters[idx][0] - x_axis)
        yshift = np.abs(allcenters[idx][1] - y_axis)
        if (xshift <= center_margin) & (yshift <= center_margin):
            x_radius = np.max((x_radius, allradii[idx] + xshift))
            y_radius = np.max((y_radius, allradii[idx] + yshift))

    x_radius *= upscale
    y_radius *= upscale

    print (x_axis, y_axis), x_radius, y_radius
    return (x_axis, y_axis), (x_radius, y_radius)