# Copyright (c) 2023-2024, Zexin He
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import torch
import torch.nn as nn
from accelerate.logging import get_logger

from .embedder import CameraEmbedder
from .transformer import TransformerDecoder
from .rendering.synthesizer import TriplaneSynthesizer
from .utils import zero_module
import loratorch as lora
from .swin_transformer import CrossAttentionLayer

logger = get_logger(__name__)


class ModelLRM(nn.Module):
    """
    Full model of the basic single-view large reconstruction model.
    """
    def __init__(self, camera_embed_dim: int, rendering_samples_per_ray: int,
                 transformer_dim: int, transformer_layers: int, transformer_heads: int,
                 triplane_low_res: int, triplane_high_res: int, triplane_dim: int,
                 encoder_freeze: bool = True, encoder_type: str = 'dino',
                 encoder_model_name: str = 'facebook/dino-vitb16', encoder_feat_dim: int = 768,
                 model_lora_rank: int = 0, conv_fuse=False,
                 swin_ca_fuse=False, ca_dim=32, ca_depth=2, ca_num_heads=8, ca_window_size=2):
        super().__init__()
        
        # attributes
        self.encoder_feat_dim = encoder_feat_dim
        self.camera_embed_dim = camera_embed_dim
        self.triplane_low_res = triplane_low_res
        self.triplane_high_res = triplane_high_res
        self.triplane_dim = triplane_dim

        self.conv_fuse = conv_fuse
        self.swin_ca_fuse = swin_ca_fuse

        # modules
        self.encoder = self._encoder_fn(encoder_type)(
            model_name=encoder_model_name,
            freeze=encoder_freeze,
        )
        self.camera_embedder = CameraEmbedder(
            raw_dim=12+4, embed_dim=camera_embed_dim,
        )
        # initialize pos_embed with 1/sqrt(dim) * N(0, 1)
        self.pos_embed = nn.Parameter(torch.randn(1, 3*triplane_low_res**2, transformer_dim) * (1. / transformer_dim) ** 0.5)
        if model_lora_rank > 0:
            self.transformer = TransformerDecoder(
                block_type='cond_mod',
                num_layers=transformer_layers, num_heads=transformer_heads,
                inner_dim=transformer_dim, cond_dim=encoder_feat_dim, mod_dim=camera_embed_dim,
                lora_rank=model_lora_rank
            )
            lora.mark_only_lora_as_trainable(self.transformer)
        else:
            self.transformer = TransformerDecoder(
                block_type='cond_mod',
                num_layers=transformer_layers, num_heads=transformer_heads,
                inner_dim=transformer_dim, cond_dim=encoder_feat_dim, mod_dim=camera_embed_dim,
            )
        self.upsampler = nn.ConvTranspose2d(transformer_dim, triplane_dim, kernel_size=2, stride=2, padding=0)
        self.synthesizer = TriplaneSynthesizer(
            triplane_dim=triplane_dim, samples_per_ray=rendering_samples_per_ray,
        )

        if model_lora_rank > 0:
            if self.conv_fuse:
                # self.front_back_conv = nn.Conv2d(in_channels=triplane_dim*2, out_channels=triplane_dim, kernel_size=(3, 3), stride=(1, 1), padding=1)
                # zero_module(self.front_back_conv)
                self.front_back_conv = nn.ModuleList([
                        nn.Conv2d(in_channels=triplane_dim*2, out_channels=triplane_dim*4, kernel_size=(3, 3), stride=(1, 1), padding=1),
                        nn.LayerNorm([triplane_dim*4, triplane_high_res, triplane_high_res]),  # Using Layer Normalization
                        nn.GELU(),  # Using GELU activation
                        nn.Conv2d(in_channels=triplane_dim*4, out_channels=triplane_dim*4, kernel_size=(3, 3), stride=(1, 1), padding=1),
                        nn.LayerNorm([triplane_dim*4, triplane_high_res, triplane_high_res]),  # Using Layer Normalization
                        nn.GELU(),  # Using GELU activation
                        nn.Conv2d(in_channels=triplane_dim*4, out_channels=triplane_dim, kernel_size=(3, 3), stride=(1, 1), padding=1)
                    ])
                self.freeze_modules(encoder=True, camera_embedder=True, 
                                        pos_embed=False, transformer=False, upsampler=False,
                                        synthesizer=False) 
            elif self.swin_ca_fuse:
                self.swin_cross_attention = CrossAttentionLayer(dim=ca_dim, depth=ca_depth, num_heads=ca_num_heads, window_size=ca_window_size)
                self.freeze_modules(encoder=True, camera_embedder=True, 
                                        pos_embed=False, transformer=False, upsampler=False,
                                        synthesizer=False)
            else:
                raise ValueError("You need to specify a method for fusing the front and the back.")


    def freeze_modules(self, encoder=False, camera_embedder=False, 
                        pos_embed=False, transformer=False, upsampler=False, 
                        synthesizer=False):
        """
        Freeze specified modules
        """
        if encoder:
            for param in self.encoder.parameters():
                param.requires_grad = False
        if camera_embedder:
            for param in self.camera_embedder.parameters():
                param.requires_grad = False
        if pos_embed:
            for param in self.pos_embed.parameters():
                param.requires_grad = False
        if transformer:
            for param in self.transformer.parameters():
                param.requires_grad = False
        if upsampler:
            for param in self.upsampler.parameters():
                param.requires_grad = False
        if synthesizer:
            for param in self.synthesizer.parameters():
                param.requires_grad = False

    @staticmethod
    def _encoder_fn(encoder_type: str):
        encoder_type = encoder_type.lower()
        assert encoder_type in ['dino', 'dinov2'], "Unsupported encoder type"
        if encoder_type == 'dino':
            from .encoders.dino_wrapper import DinoWrapper
            logger.info("Using DINO as the encoder")
            return DinoWrapper
        elif encoder_type == 'dinov2':
            from .encoders.dinov2_wrapper import Dinov2Wrapper
            logger.info("Using DINOv2 as the encoder")
            return Dinov2Wrapper

    def forward_transformer(self, image_feats, camera_embeddings):
        assert image_feats.shape[0] == camera_embeddings.shape[0], \
            "Batch size mismatch for image_feats and camera_embeddings!"
        N = image_feats.shape[0]
        x = self.pos_embed.repeat(N, 1, 1)  # [N, L, D]
        x = self.transformer(
            x,
            cond=image_feats,
            mod=camera_embeddings,
        )
        return x

    def reshape_upsample(self, tokens):
        N = tokens.shape[0]
        H = W = self.triplane_low_res
        x = tokens.view(N, 3, H, W, -1)
        x = torch.einsum('nihwd->indhw', x)  # [3, N, D, H, W]
        x = x.contiguous().view(3*N, -1, H, W)  # [3*N, D, H, W]
        x = self.upsampler(x)  # [3*N, D', H', W']
        x = x.view(3, N, *x.shape[-3:])  # [3, N, D', H', W']
        x = torch.einsum('indhw->nidhw', x)  # [N, 3, D', H', W']
        x = x.contiguous()
        return x

    @torch.compile
    def forward_planes(self, image, camera):
        # image: [N, C_img, H_img, W_img]
        # camera: [N, D_cam_raw]
        N = image.shape[0]

        # encode image
        image_feats = self.encoder(image)
        assert image_feats.shape[-1] == self.encoder_feat_dim, \
            f"Feature dimension mismatch: {image_feats.shape[-1]} vs {self.encoder_feat_dim}"

        # embed camera
        camera_embeddings = self.camera_embedder(camera)
        assert camera_embeddings.shape[-1] == self.camera_embed_dim, \
            f"Feature dimension mismatch: {camera_embeddings.shape[-1]} vs {self.camera_embed_dim}"

        # transformer generating planes
        tokens = self.forward_transformer(image_feats, camera_embeddings)
        planes = self.reshape_upsample(tokens)
        assert planes.shape[0] == N, "Batch size mismatch for planes"
        assert planes.shape[1] == 3, "Planes should have 3 channels"

        return planes

    def forward(self, image, source_camera, render_cameras, render_anchors, render_resolutions, render_bg_colors, render_region_size: int,
                image_back=None,):
        # image: [N, C_img, H_img, W_img]
        # source_camera: [N, D_cam_raw]
        # render_cameras: [N, M, D_cam_render]
        # render_anchors: [N, M, 2]
        # render_resolutions: [N, M, 1]
        # render_bg_colors: [N, M, 1]
        # render_region_size: int
        assert image.shape[0] == source_camera.shape[0], "Batch size mismatch for image and source_camera"
        assert image.shape[0] == render_cameras.shape[0], "Batch size mismatch for image and render_cameras"
        assert image.shape[0] == render_anchors.shape[0], "Batch size mismatch for image and render_anchors"
        assert image.shape[0] == render_bg_colors.shape[0], "Batch size mismatch for image and render_bg_colors"
        N, M = render_cameras.shape[:2]

        if image_back is not None:
            front_planes = self.forward_planes(image, source_camera)
            back_planes = self.forward_planes(image_back, source_camera)

            # XY Plane
            back_planes[:, 0, :, :, :] = torch.flip(back_planes[:, 0, :, :, :], dims=[-2, -1])
            # XZ Plane
            back_planes[:, 1, :, :, :] = torch.flip(back_planes[:, 1, :, :, :], dims=[-1])
            # YZ Plane
            back_planes[:, 2, :, :, :] = torch.flip(back_planes[:, 2, :, :, :], dims=[-2])
        
            # To fuse the front planes and the back planes
            bs, num_planes, channels, height, width = front_planes.shape
            if self.conv_fuse:
                planes = torch.cat((front_planes, back_planes), dim=2)
                planes = planes.reshape(-1, channels*2, height, width) 
                # Apply multiple convolutional layers
                for layer in self.front_back_conv:
                    planes = layer(planes)
                
                planes = planes.view(bs, num_planes, -1, height, width)
                # planes = self.front_back_conv(planes).view(bs, num_planes, -1, height, width)  # only one layer.
            elif self.swin_ca_fuse:
                front_planes = front_planes.reshape(bs*num_planes, channels, height*width).permute(0, 2, 1).contiguous()    # [8, 3, 32, 64, 64] -> [24, 32, 4096] -> [24, 4096, 32]
                back_planes = back_planes.reshape(bs*num_planes, channels, height*width).permute(0, 2, 1).contiguous()
                planes = self.swin_cross_attention(front_planes, back_planes, height, width)[0].permute(0, 2, 1).reshape(bs, num_planes, channels, height, width)
        else:
            planes = self.forward_planes(image, source_camera)

        # render target views
        render_results = self.synthesizer(planes, render_cameras, render_anchors, render_resolutions, render_bg_colors, render_region_size)
        assert render_results['images_rgb'].shape[0] == N, "Batch size mismatch for render_results"
        assert render_results['images_rgb'].shape[1] == M, "Number of rendered views should be consistent with render_cameras"

        return {
            'planes': planes,
            **render_results,
        }
