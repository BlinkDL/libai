from libai.config import LazyCall

from projects.SegFormer.modeling.mix_transformer_segmentation import MixVisionTransformerForSegmentation
from projects.SegFormer.configs.models.mit_b0 import cfg

cfg.embed_dims=[64, 128, 320, 512]
cfg.decoder_in_channels=[64, 128, 320, 512]


model = LazyCall(MixVisionTransformerForSegmentation)(cfg=cfg)
