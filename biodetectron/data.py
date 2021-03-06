import os
import copy
import torch
import numpy as np
import pandas as pd
from glob import glob
from pycocotools import mask as cocomask
from skimage.io import imread
from skimage.measure import regionprops
from skimage.exposure import rescale_intensity

from imgaug.augmentables.bbs import BoundingBox, BoundingBoxesOnImage

from detectron2.structures import BoxMode
from detectron2.data import transforms as T
from detectron2.evaluation import DatasetEvaluator
from detectron2.data import DatasetMapper, MetadataCatalog, detection_utils as utils

from biodetectron.datasets import get_custom_augmenters


def get_csv(root_dir, dataset, suffix='', scaling=True, do_mapping=True):
    imglist = glob(os.path.join(root_dir, '*.jpg')) + \
                    glob(os.path.join(root_dir, '*.tif')) + \
                    glob(os.path.join(root_dir, '*.png'))

    imglist.sort()

    dataset_dicts = []
    for idx, filename in enumerate(imglist):
        record = {}

        record["file_name"] = filename
        record["image_id"] = idx

        targets = pd.read_csv(imglist[idx].replace('.jpg', suffix + '.csv').replace('.tif', suffix + '.csv').replace('.png', suffix + '.csv'))

        if do_mapping:
            try:
                mapping = MetadataCatalog.get(dataset).thing_dataset_id_to_contiguous_id
            except:
                mapping = None
        else:
            mapping = None

        objs = []
        for row in targets.itertuples():
            category_id = mapping[row.category_id] if mapping is not None else row.category_id

            x1 = row.x1
            x2 = row.x2
            y1 = row.y1
            y2 = row.y2
            
            if scaling:
                scaling_factor = 0.5

                width = x2 - x1
                height = y2 - y1

                x1 = x1 + width * scaling_factor / 2
                x2 = x2 - width * scaling_factor / 2
                y1 = y1 + width * scaling_factor / 2
                y2 = y2 - width * scaling_factor / 2

            obj = {
                "bbox": [x1, y1, x2, y2],
                "bbox_mode": BoxMode.XYXY_ABS,
                "segmentation": [],
                "category_id":  category_id,
                "iscrowd": 0
            }
            objs.append(obj)
        record["annotations"] = objs
        dataset_dicts.append(record)

    return dataset_dicts


class BoxDetectionLoader(DatasetMapper):
    def __init__(self, cfg, is_train=True):
        super().__init__(cfg, is_train=is_train)
        self.cfg = cfg

    def __call__(self, dataset_dict):
        """
        Args:
            dataset_dict (dict): Metadata of one image, in Detectron2 Dataset format.
        Returns:
            dict: a format that builtin models in detectron2 accept
        """
        dataset_dict = copy.deepcopy(dataset_dict)  # it will be modified by code below

        # Read image and reshape it to always be [h, w, 3].
        image = imread(dataset_dict["file_name"])
        if len(image.shape) > 3:
            image = np.max(image, axis=0)
        if len(image.shape) < 3:
            image = np.expand_dims(image, axis=-1)
        if image.shape[0] < image.shape[-1]:
            image = np.transpose(image, (1, 2, 0))
        if image.shape[-1] == 1:
            image = np.repeat(image, 3, axis=-1)

        image_shape = image.shape[:2]  # h, w
        dataset_dict['height'] = image_shape[0]
        dataset_dict['width'] = image_shape[1]

        if 0 in self.cfg.MODEL.PIXEL_MEAN and 1 in self.cfg.MODEL.PIXEL_STD:
            image = image.astype(np.float32)
            image = rescale_intensity(image)

        if not self.is_train:
            dataset_dict['gt_image'] = np.expand_dims(image[:,:,0], axis=-1)

        # Convert bounding boxes to imgaug format for augmentation.
        boxes = []
        for anno in dataset_dict["annotations"]:
            # if iscrowd == 0 ?
            boxes.append(BoundingBox(
                x1=anno["bbox"][0], x2=anno["bbox"][2],
                y1=anno["bbox"][1], y2=anno["bbox"][3], label=anno["category_id"]))

        boxes = BoundingBoxesOnImage(boxes, shape=image_shape)

        # Define augmentations.
        seq = get_custom_augmenters(
            self.cfg.DATASETS.TRAIN,
            self.cfg.INPUT.MAX_SIZE_TRAIN,
            self.is_train,
            image_shape
        )

        if self.is_train:
            image, boxes = seq(image=image, bounding_boxes=boxes)
        else:
            image, _ = seq(image=image, bounding_boxes=boxes)


        # Convert image to tensor for pytorch model.
        dataset_dict["image"] = torch.as_tensor(image.transpose(2, 0, 1).astype("float32"))

        # Convert boxes back to detectron2 annotation format.
        annos = []
        for box in boxes:
            obj = {
                "bbox": [box.x1, box.y1, box.x2, box.y2],
                "bbox_mode": BoxMode.XYXY_ABS,
                "segmentation": [],
                "category_id":  box.label,
                "iscrowd": 0
            }
            annos.append(obj)

        # Convert bounding box annotations to instances.
        instances = utils.annotations_to_instances(
            annos, image_shape, mask_format=self.mask_format
        )

        # Create a tight bounding box from masks, useful when image is cropped
        if self.crop_gen and instances.has("gt_masks"):
            instances.gt_boxes = instances.gt_masks.get_bounding_boxes()

        dataset_dict["instances"] = utils.filter_empty_instances(instances, by_mask=False)

        return dataset_dict


def get_masks(root_dir):
    imglist = glob(os.path.join(root_dir, '*.jpg')) + \
              glob(os.path.join(root_dir, '*.tif')) + \
              glob(os.path.join(root_dir, '*.png'))

    imglist.sort()

    dataset_dicts = []
    for idx, filename in enumerate(imglist):
        record = {}
        objs = []

        ### THIS IS UNEFFICIENT
        height, width = imread(filename).shape[1:3]

        record["file_name"] = filename
        record["image_id"] = idx
        record["height"] = height
        record["width"] = width

        splitname = os.path.splitext(filename.replace('train', 'masks').replace('val', 'masks'))

        #### ATP6_NG
        mask = imread(splitname[0] + '_ATP6-NG' + splitname[1])
        rle_mask = cocomask.encode(np.asfortranarray(mask))

        fullmask = np.zeros_like(mask, dtype=np.int16)
        fullmask[mask!=0] = 1 

        box = regionprops(mask)[0].bbox
        box = [box[1], box[0], box[3], box[2]]

        obj = {
            "bbox": box,
            "bbox_mode": BoxMode.XYXY_ABS,
            "segmentation": rle_mask,
            "category_id": 0,
            "iscrowd": 0
        }
        objs.append(obj)

        ##### mtKate2
        mask = imread(splitname[0] + '_mtKate2' + splitname[1])
        rle_mask = cocomask.encode(np.asfortranarray(mask))

        fullmask[mask!=0] = 2

        box = regionprops(mask)[0].bbox
        box = [box[1], box[0], box[3], box[2]]

        obj = {
            "bbox": box,
            "bbox_mode": BoxMode.XYXY_ABS,
            "segmentation": rle_mask,
            "category_id": 1,
            "iscrowd": 0
        }
        objs.append(obj)

        ##### daughter
        mask = imread(splitname[0] + '_daughter' + splitname[1])
        rle_mask = cocomask.encode(np.asfortranarray(mask))

        box = regionprops(mask)[0].bbox
        box = [box[1], box[0], box[3], box[2]]

        fullmask[mask!=0] = 3

        obj = {
            "bbox": box,
            "bbox_mode": BoxMode.XYXY_ABS,
            "segmentation": rle_mask,
            "category_id": 2,
            "iscrowd": 0
        }
        objs.append(obj)

        record["annotations"] = objs
        record["sem_seg"] = torch.as_tensor(fullmask).long()
        dataset_dicts.append(record)

    return dataset_dicts


class MaskDetectionLoader(DatasetMapper):
    def __init__(self, cfg, is_train=True, mask_format="bitmask"):
        super().__init__(cfg, is_train=is_train)
        self.cfg = cfg
        self.mask_format=mask_format

    def __call__(self, dataset_dict):
        """
        Args:
            dataset_dict (dict): Metadata of one image, in Detectron2 Dataset format.

        Returns:
            dict: a format that builtin models in detectron2 accept
        """
        dataset_dict = copy.deepcopy(dataset_dict)  # it will be modified by code below

        # Read image and reshape it to always be [h, w, 3].
        image = imread(dataset_dict["file_name"])

        ### NOT GENERALIZED YET!
        if len(image.shape) > 3:
            image = np.max(image, axis=0)
        elif len(image.shape) < 3:
            image = np.expand_dims(image, axis=-1)
        if image.shape[0] < image.shape[-1]:
            image = np.transpose(image, (1, 2, 0))
        if image.shape[-1] == 1:
            image = np.repeat(image, 3, axis=-1)

        utils.check_image_size(dataset_dict, image)
        image_shape = image.shape[:2]  # h, w

        if 0 in self.cfg.MODEL.PIXEL_MEAN and 1 in self.cfg.MODEL.PIXEL_STD:
            image = image.astype(np.float32)
            image = rescale_intensity(image)

        if not self.is_train:
            dataset_dict['gt_image'] = image

        # Convert bounding boxes to imgaug format for augmentation.
        boxes = []
        for anno in dataset_dict["annotations"]:
            # if iscrowd == 0 ?
            boxes.append(BoundingBox(
                x1=anno["bbox"][0], x2=anno["bbox"][2],
                y1=anno["bbox"][1], y2=anno["bbox"][3], label=anno["category_id"]))

        boxes = BoundingBoxesOnImage(boxes, shape=image_shape)

        # Define augmentations.
        seq = get_custom_augmenters(
            self.cfg.DATASETS.TRAIN,
            self.cfg.INPUT.MAX_SIZE_TRAIN,
            self.is_train,
            image_shape
        )

        if self.is_train:
            image, boxes = seq(image=image, bounding_boxes=boxes)
        else:
            image, _ = seq(image=image, bounding_boxes=boxes)


        # Convert image to tensor for pytorch model.
        dataset_dict["image"] = torch.as_tensor(image.transpose(2, 0, 1).astype("float32"))

        # Convert boxes back to detectron2 annotation format.
        annos = []
        for n, box in enumerate(boxes):
            obj = {
                "bbox": [box.x1, box.y1, box.x2, box.y2],
                "bbox_mode": BoxMode.XYXY_ABS,
                "segmentation": dataset_dict["annotations"][n]["segmentation"],
                "category_id":  box.label,
                "iscrowd": 0
            }
            annos.append(obj)

        # Convert bounding box annotations to instances.
        instances = utils.annotations_to_instances(
            annos, image_shape, mask_format=self.mask_format
        )

        # Create a tight bounding box from masks, useful when image is cropped
        if self.crop_gen and instances.has("gt_masks"):
            instances.gt_boxes = instances.gt_masks.get_bounding_boxes()

        dataset_dict["instances"] = utils.filter_empty_instances(instances, by_mask=False)

        return dataset_dict
