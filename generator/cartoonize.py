"""可选卡通化模块 — 接入开源项目 minivision-ailab/photo2cartoon。

该项目用 InsightFace 提取人脸 ID 特征，约束 GAN 生成保留本人长相的
Q版卡通头像，MIT 协议开源:
    https://github.com/minivision-ailab/photo2cartoon

接入步骤:
1. git clone https://github.com/minivision-ailab/photo2cartoon
2. 按 README 下载 photo2cartoon_weights.pt 放入其 models/ 目录
3. 将下方 use_photo2cartoon 设为 True，并把 PHOTO2CARTOON_DIR 指向克隆目录
4. pip install face-alignment dlib (项目要求，仅 CPU 也可运行推理)

未配置时 stylize() 原样返回输入，生成器自动退回抠图路线。
"""
import os

use_photo2cartoon = False
PHOTO2CARTOON_DIR = os.environ.get("PHOTO2CARTOON_DIR", "../photo2cartoon")


def stylize(img):
    """输入/输出均为 PIL.Image (RGB)。"""
    if not use_photo2cartoon:
        return img
    import sys
    sys.path.insert(0, PHOTO2CARTOON_DIR)
    import numpy as np
    from test import Photo2Cartoon  # photo2cartoon 的推理入口

    p2c = Photo2Cartoon()
    arr = np.array(img)
    cartoon = p2c.inference(arr)  # 返回 BGR ndarray
    from PIL import Image
    return Image.fromarray(cartoon[:, :, ::-1])
