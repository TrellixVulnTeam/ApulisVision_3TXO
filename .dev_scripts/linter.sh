yapf -r -i mmdet/ mmcls/ mmseg/ configs/ tests/ tools/
isort -rc mmdet/ mmcls/ mmseg/ configs/ tests/ tools/
flake8 .
