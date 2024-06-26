# some codes from CLIP github(https://github.com/openai/CLIP), from VideoMAE github(https://github.com/MCG-NJU/VideoMAE)
from functools import partial
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from timm.models.layers import drop_path, to_2tuple, trunc_normal_
from timm.models.registry import register_model
from collections import OrderedDict
from einops import rearrange
import random
import math


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 400, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': .9, 'interpolation': 'bicubic',
        'mean': (0.5, 0.5, 0.5), 'std': (0.5, 0.5, 0.5),
        **kwargs
    }

class DropPath(nn.Module):
    """Drop paths (Stochastic Depth) per sample  (when applied in main path of residual blocks).
    """
    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)
    
    def extra_repr(self) -> str:
        return 'p={}'.format(self.drop_prob)
    
class LayerNorm(nn.LayerNorm):
    """Subclass torch's LayerNorm to handle fp16."""
    def forward(self, x: torch.Tensor):
        orig_type = x.dtype
        if orig_type == torch.float16:
            ret = super().forward(x)
        elif orig_type == torch.float32:
            ret = super().forward(x.type(torch.float32))
        return ret.type(orig_type)

class QuickGELU(nn.Module):
    def forward(self, x: torch.Tensor):
        return x * torch.sigmoid(1.702 * x)

class PatchEmbed(nn.Module):
    """ Image to Patch Embedding
    """
    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768, num_frames=16, tubelet_size=2):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        self.tubelet_size = int(tubelet_size)
        num_patches = (img_size[1] // patch_size[1]) * (img_size[0] // patch_size[0]) * (num_frames // self.tubelet_size)
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches
        self.proj = nn.Conv3d(in_channels=in_chans, out_channels=embed_dim, 
                            kernel_size = (self.tubelet_size,  patch_size[0],patch_size[1]), 
                            stride=(self.tubelet_size,  patch_size[0],  patch_size[1]))

    def forward(self, x, **kwargs):
        B, C, T, H, W = x.shape
        # FIXME look at relaxing size constraints
        assert H == self.img_size[0] and W == self.img_size[1], \
            f"Input image size ({H}*{W}) doesn't match model ({self.img_size[0]}*{self.img_size[1]})."
        x = self.proj(x).flatten(2).transpose(1, 2)
        return x
    
# sin-cos position encoding
# https://github.com/jadore801120/attention-is-all-you-need-pytorch/blob/master/transformer/Models.py#L31
def get_sinusoid_encoding_table(n_position, d_hid): 
    ''' Sinusoid position encoding table ''' 
    # TODO: make it with torch instead of numpy 
    def get_position_angle_vec(position): 
        return [position / np.power(10000, 2 * (hid_j // 2) / d_hid) for hid_j in range(d_hid)] 

    sinusoid_table = np.array([get_position_angle_vec(pos_i) for pos_i in range(n_position)]) 
    sinusoid_table[:, 0::2] = np.sin(sinusoid_table[:, 0::2]) # dim 2i 
    sinusoid_table[:, 1::2] = np.cos(sinusoid_table[:, 1::2]) # dim 2i+1 

    return torch.FloatTensor(sinusoid_table).unsqueeze(0) 

class Mlp(nn.Module):
    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        # x = self.drop(x)
        # commit this for the orignal BERT implement 
        x = self.fc2(x)
        x = self.drop(x)
        return x


class Attention(nn.Module):
    def __init__(
            self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0.,
            proj_drop=0., attn_head_dim=None):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        if attn_head_dim is not None:
            head_dim = attn_head_dim
        all_head_dim = head_dim * self.num_heads
        self.scale = qk_scale or head_dim ** -0.5

        self.qkv = nn.Linear(dim, all_head_dim * 3, bias=False)
        if qkv_bias:
            self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
            self.v_bias = nn.Parameter(torch.zeros(all_head_dim))
        else:
            self.q_bias = None
            self.v_bias = None

        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(all_head_dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv_bias = None
        if self.q_bias is not None:
            qkv_bias = torch.cat((self.q_bias, torch.zeros_like(self.v_bias, requires_grad=False), self.v_bias))
        # qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        qkv = F.linear(input=x, weight=self.qkv.weight, bias=qkv_bias)
        qkv = qkv.reshape(B, N, 3, self.num_heads, -1).permute(2, 0, 3, 1, 4)
        s2t_q, k, v = qkv[0], qkv[1], qkv[2]   # make torchscript happy (cannot use tensor as tuple)

        s2t_q = s2t_q * self.scale
        attn = (s2t_q @ k.transpose(-2, -1))

        
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).transpose(1, 2).reshape(B, N, -1)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x
    
class Block(nn.Module):
    def __init__(self, dim, num_heads, num_frames=16, mlp_ratio=4., down_ratio=2, qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., init_values=None, num_layer=0, act_layer=nn.GELU, norm_layer=nn.LayerNorm, attn_head_dim=None, 
                 spec_frames=1, attn_all_frame=True, CA=0, use_Adapter=True, audio_patch=196, use_AIM=False, audio_only=False,
                 late_fusion=0, use_SA=True, use_MLP=True, time_encoding=False, spec_shape=None):
        super().__init__()
        self.num_layer = num_layer
        self.num_heads = num_heads
        self.down_ratio = down_ratio
        self.scale = 0.5
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.act = act_layer()
        self.num_frames = num_frames//2
        self.CA = CA
        ###################################### MHSA code #####################################
        ############################ CLIP MHSA ###########################
        self.clip_ln_1 = LayerNorm(dim)
        self.clip_attn = nn.MultiheadAttention(dim, num_heads)
        ##################################################################
        
        ###################################### FFN code #########################################
        ############################ CLIP FFN ###############################
        self.clip_ln_2 = LayerNorm(dim)
        self.clip_mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(dim, dim * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(dim * 4, dim))
        ]))
        self.attn_mask = None
        #########################################################################################
        

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        
    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.clip_attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self,s_x, t_x, time_encodings=None, output_attentions=None):
        ############################ MHSA Forward #############################
        s_x = s_x + self.attention(self.clip_ln_1(s_x))
        ########################################################################
        
        ############################ FFN Forward ##################################
        s_xn = self.clip_ln_2(s_x)
        s_x = s_x + self.clip_mlp(s_xn)
        ############################################################################
        return (s_x, t_x)
    
class STCrossTransformer(nn.Module):
    """ Vision Transformer with support for patch or hybrid CNN input stage
    """
    def __init__(self, 
                 img_size=224, 
                 patch_size=16, 
                 in_chans=3, 
                 num_classes=1000, 
                 embed_dim=768, 
                 depth=12,
                 num_heads=12, 
                 mlp_ratio=4.,
                 down_ratio=2,
                 qkv_bias=False, 
                 qk_scale=None, 
                 drop_rate=0., 
                 attn_drop_rate=0.,
                 drop_path_rate=0.,
                 head_drop_rate=0.,
                 norm_layer=nn.LayerNorm, 
                 init_values=0.,
                 use_learnable_pos_emb=False,
                 init_scale=0.,
                 all_frames=16,
                 tubelet_size=2,
                 use_mean_pooling=True,
                 composition=False,
                 fusion_method=None,
                 audio_enabled=False,
                 spec_frames=1,
                 attn_all_frame=True,
                 CA=0,
                 use_Adapter=True,
                 audio_patch=196,
                 use_AIM=False,
                 audio_only=False,
                 late_fusion=0,
                 use_SA=True, 
                 use_MLP=True,
                 time_encoding=False,
                 spec_shape=None,
                 pretrained_cfg = None,
                 pretrained_cfg_overlay = None):
        super().__init__()
        self.num_classes = num_classes
        self.num_frames = all_frames
        self.spec_frames = spec_frames
        self.embed_dim = embed_dim  # num_features for consistency with other models
        self.tubelet_size = tubelet_size
        self.down_ratio = down_ratio
        self.composition = composition
        self.audio_enabled = audio_enabled
        self.audio_patch = audio_patch
        self.spec_shape = spec_shape
        self.video_patch = (img_size // patch_size) ** 2
        
        scale = embed_dim ** -0.5
        self.clip_conv1 = nn.Conv2d(in_channels=3, out_channels=embed_dim, kernel_size=patch_size, stride=patch_size, bias=False)
        self.clip_class_embedding = nn.Parameter(scale * torch.randn(embed_dim))
        self.clip_text_positional_embedding = nn.Parameter(scale * torch.randn(audio_patch + 1, embed_dim))
        self.clip_ln_pre = LayerNorm(embed_dim)

        ###################################
        self.audio_only = audio_only
        self.use_AIM = use_AIM
        if audio_only:
            self.num_frames = 2
            all_frames = 2
        spec_frames = (spec_frames+1) //2
        attn_all_frame=attn_all_frame
        CA=CA
        # self.num_frames = all_frames * 2
        # CA=12
        ###################################

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, num_frames=self.num_frames, mlp_ratio=mlp_ratio,down_ratio=self.down_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                init_values=init_values, num_layer=i, spec_frames=spec_frames, attn_all_frame=attn_all_frame, CA=CA, use_Adapter=use_Adapter,late_fusion=late_fusion, use_SA=use_SA, use_MLP=use_MLP,
                audio_patch=audio_patch, use_AIM=use_AIM, audio_only=audio_only)
            for i in range(depth)])
        
        self.audio_ln_post = LayerNorm(embed_dim)
        self.clip_ln_post = LayerNorm(embed_dim)
        
        if self.composition:
            self.head_verb = nn.Linear(embed_dim, 97)
            self.head_verb_dropout = nn.Dropout(head_drop_rate)
            self.head_noun = nn.Linear(embed_dim, 300)
            self.head_noun_dropout = nn.Dropout(head_drop_rate)
        else:
            self.head = nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()
            self.head_dropout = nn.Dropout(head_drop_rate)

        if use_learnable_pos_emb:
            trunc_normal_(self.pos_embed, std=.02)

        self.apply(self._init_weights)
        
        if self.composition:
            self.head_verb.weight.data.mul_(init_scale)
            self.head_verb.bias.data.mul_(init_scale)
            self.head_noun.weight.data.mul_(init_scale)
            self.head_noun.bias.data.mul_(init_scale)
        else:
            self.head.weight.data.mul_(init_scale)
            self.head.bias.data.mul_(init_scale)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)

        

    def get_num_layers(self):
        return len(self.blocks)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'clip_time_pos','clip_space_pos','vmae_space_pos','vmae_time_pos','pos_embed'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()
    
    def forward_features(self, x, spec=None, time_encodings=None, output_attentions=None):
        B = x.shape[0]
        s_x = spec[:, :, 1::2, :, :] if spec.dim() == 5 else spec.unsqueeze(2)
        # s_x = torch.randn_like(x[:, :, 1::2, :, :]) # pick even frames
        ######################## Audio spatial path #########################
        n, s_t = s_x.shape[1], s_x.shape[2]
        s_x = rearrange(s_x, 'b c t h w -> (b t) c h w')
        s_x = self.clip_conv1(s_x) # shape = [*, embeddim, grid, grid]
        s_x = s_x.reshape(s_x.shape[0], s_x.shape[1], -1) # [*, embeddim, grid**2]
        s_x = s_x.permute(0, 2, 1) # shape[batch, patchnum, embeddim]
        s_x = torch.cat([self.clip_class_embedding.to(s_x.dtype) + torch.zeros(s_x.shape[0], 1, s_x.shape[-1], dtype=s_x.dtype, device=s_x.device), s_x], dim=1)
        s_x = s_x + self.clip_text_positional_embedding.to(s_x.dtype)
        s_x = self.clip_ln_pre(s_x)
        #####################################################################
    
        s_x = s_x.permute(1,0,2)
        all_self_attentions = () if output_attentions is not None else None
        for blk in self.blocks:
            layer_outputs = blk(s_x, None, time_encodings, output_attentions=output_attentions)
            s_x, t_x = layer_outputs[0], layer_outputs[1]
        s_x = s_x.permute(1,0,2)
        
        s_x = rearrange(s_x, '(b t) n d -> b t n d', b=B)
        s_x = self.audio_ln_post(s_x[:,:,0,:].mean(1)) # all cls tokens avg pooling
        
        return s_x, t_x, all_self_attentions

    def forward(self, x, caption=None, spec=None, idx=None, output_attentions=None):
        time_encodings=None
        if self.composition:
            x, t_x, all_self_attentions = self.forward_features(spec, spec, output_attentions=output_attentions)
            s_x = self.head_noun_dropout(x)
            s_x = self.head_noun(s_x)
            t_x = self.head_verb_dropout(x)
            t_x = self.head_verb(t_x)
            return s_x, t_x
        else:
            s_x, t_x, all_self_attentions = self.forward_features(spec, spec, output_attentions=output_attentions)
            x = self.head_dropout(s_x)
            x = self.head(x)
            return x

# @register_model
# def bidir_vit_base_patch16_224(pretrained=False, **kwargs):
#     model = STCrossTransformer(
#         patch_size=16, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=False, **kwargs)
#     return model

# @register_model
# def compo_bidir_vit_base_patch16_224(pretrained=False, **kwargs):
#     model = STCrossTransformer(
#         patch_size=16, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=True, **kwargs)
#     return model

@register_model
def audio_vit_base_patch16_224(pretrained=False, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=False, audio_enabled=True, 
        CA=0, spec_frames=1, attn_all_frame=True, audio_only=True, **kwargs)
    return model

@register_model
def compo_audio_vit_base_patch16_224(pretrained=False, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=True, audio_enabled=True, 
        CA=0, spec_frames=1, attn_all_frame=True, audio_only=True, **kwargs)
    return model