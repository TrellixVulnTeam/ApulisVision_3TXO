# Import the required modules
import cloudpickle as pickle
from mmcls.apis import custom_inference_classfication, init_classfication
from mmseg.apis import custom_inference_segmentor, init_segmentor
from mmdet.apis import custom_inference_detector, init_detector
import warnings

import matplotlib.pyplot as plt
import mmcv
import numpy as np
import torch
from mmcv.ops import RoIAlign, RoIPool
from mmcv.parallel import collate, scatter
from mmcv.runner import load_checkpoint

from mmdet.core import get_classes
from mmdet.datasets.pipelines import Compose
from mmdet.models import build_detector


class InferenceModel:

	def __init__(self, model, predict):
		self.model = model
		self.predict = predict


def pickle_dump(obj, file):
	pickled_lambda = pickle.dumps(obj)
	with open(file, 'wb') as f:
		f.write(pickled_lambda)


def pickle_load(file):
	with open(file, 'rb') as f:
		model_s = f.read()
	return pickle.loads(model_s)


def save_image(data):
	imgname = "temp.jpg"
	with open(imgname, 'wb')as f:
		f.write(data)
	return imgname


def dump_infer_model(checkpoint_file, config_file, output_file, labelfile, target='det',
                     device='cuda:0'):
	print("------------------------------")
	print("START EXPORT MODEL")
	print("------------------------------")
	if target == 'det':
		model = init_detector(config=config_file, checkpoint=checkpoint_file, device=device)
		infer_model = InferenceModel(model, custom_inference_detector)
	elif target == 'cls':
		model = init_classfication(config=config_file, checkpoint=checkpoint_file, device=device)
		infer_model = InferenceModel(model, custom_inference_classfication)
	elif target == 'seg':
		model = init_segmentor(config=config_file, checkpoint=checkpoint_file, device=device)
		infer_model = InferenceModel(model, custom_inference_segmentor)
	pickle_dump(infer_model, output_file)
	writeLabels(infer_model.model.CLASSES, labelfile)
	# model_infer(output_file)
	print("------------------------------")
	print("SUCCESS EXPORT MODEL")
	print("------------------------------")


from PIL import Image
import io
import os
import json


def model_infer(pickle_file, img_bytes):
	img = Image.open(io.BytesIO(img_bytes))
	inputImg = np.asarray(img)
	infer_model = pickle_load(pickle_file)
	print(infer_model.model.CLASSES)
	result = infer_model.predict(infer_model.model, inputImg)
	print(pickle_file)
	print(result)


def writeLabels(CLASSES, label_file):
	data = []
	with open(label_file, 'w') as jsonfile:
		for key, value in enumerate(CLASSES, start=1):
			data.append({"id": int(key), "display_name": value})
			json.dump(data, jsonfile)


if __name__ == '__main__':
	# dump model
	config_file = 'configs/faster_rcnn/faster_rcnn_r50_fpn_1x_coco.py'
	checkpoint_file = 'work_dirs/faster-rcnn/faster_rcnn_r50_fpn_1x_coco_20200130-047c8118.pth'
	output_file = 'work_dirs/faster-rcnn/export_model.pkl'
	label_file = 'work_dirs/faster-rcnn/class_names.json'
	dump_infer_model(checkpoint_file, config_file, output_file, label_file, target='det')
	img_file = 'demo/demo.jpg'
	img_bytes = open(img_file, 'rb').read()
	model_infer("work_dirs/faster-rcnn/export_model.pkl", img_bytes)
	# model_infer("work_dir/cls/export_model.pkl", img_bytes)
	# model_infer("work_dir/seg/export_model.pkl", img_bytes)