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
from models import clip
from models.clip.clip import tokenize
import math
from models.beats.modules import SamePad, get_activation_fn
from models.beats.backbone import MultiheadAttention


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
    
class Adapter(nn.Module):
    def __init__(self, dim, mlp_ratio=0.25, act_layer=nn.GELU, skip_connect=True):
        super().__init__()
        self.skip_connect = skip_connect
        down_dim = int(dim * mlp_ratio)
        self.act = act_layer()
        self.D_fc1 = nn.Linear(dim, down_dim)
        self.D_fc2 = nn.Linear(down_dim, dim)
        
    def forward(self, x):
        # x is (BT, HW+1, D)
        xs = self.D_fc1(x)
        xs = self.act(xs)
        xs = self.D_fc2(xs)
        if self.skip_connect:
            x = x + xs
        else:
            x = xs
        return x
    
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

class CrossAttentionS2Audio(nn.Module):
    def __init__(self, dim: int, audio_dim: int, n_head: int, num_frames: int, spec_frames: int, attn_all_frame = False, audio_patch = 196, attn_mask: torch.Tensor = None):
        super().__init__()
        
        # add for cross-attn
        self.num_frames = num_frames//2
        self.spec_frames = spec_frames
        self.num_head = n_head
        head_dim = audio_dim // self.num_head
        self.scale = head_dim ** -0.5
        all_head_dim = head_dim * self.num_head
        self.attn_all_frame = attn_all_frame
        if not attn_all_frame:
            self.clip_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
            self.audio_space_pos = nn.Parameter(self.scale * torch.randn((audio_patch, dim)))
        else:
            # self.clip_st_pos = nn.Parameter(self.scale * torch.randn((196 * num_frames//2, dim)))
            # self.audio_st_pos = nn.Parameter(self.scale * torch.randn((audio_patch * spec_frames, dim)))
            self.clip_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
            self.audio_space_pos = nn.Parameter(self.scale * torch.randn((audio_patch, dim)))
            self.clip_temporal_pos = nn.Parameter(self.scale * torch.randn((num_frames//2, dim)))
            self.audio_temporal_pos = nn.Parameter(self.scale * torch.randn((spec_frames, dim)))
        
        self.q = nn.Linear(audio_dim, all_head_dim, bias=False)
        self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
        self.kv = nn.Linear(dim, all_head_dim * 2, bias=False) # 197 tokens(cls+patch) * num_frames
        self.kv_bias = nn.Parameter(torch.zeros(all_head_dim * 2))
        
        self.proj = nn.Linear(all_head_dim, audio_dim)
    
    def s2audio_cross_attn(self, s_x, audio): # s_x=[n (b t) d], t_x=[b (t n) d], text=[m=77 b d]
        t = self.num_frames
        s_x_pat = s_x[1:, :, :]
        audio_pat = audio
        if not self.attn_all_frame:
            s_x_pat = rearrange(s_x_pat, 'n b d -> b n d') # batch -> token
            s_x_pat = s_x_pat + self.clip_space_pos
            audio_pat = rearrange(audio_pat, 'n b d -> b n d') # batch -> token
            audio_pat = audio_pat + self.audio_space_pos
        else:
            # s_x_pat = rearrange(s_x_pat, 'n (b t) d -> b (n t) d', t=t) # batch -> token
            # s_x_pat = s_x_pat + self.clip_st_pos
            # audio_pat = rearrange(audio_pat, 'n (b t) d -> b (n t) d', t=self.spec_frames) # batch -> token
            # audio_pat = audio_pat + self.audio_st_pos
            s_x_pat = rearrange(s_x_pat, 'n (b t) d -> b t n d', t=t)
            s_x_pat = s_x_pat + self.clip_space_pos
            s_x_pat = rearrange(s_x_pat, 'b t n d -> b n t d')
            s_x_pat = s_x_pat + self.clip_temporal_pos
            s_x_pat = rearrange(s_x_pat, 'b n t d -> b (n t) d')
            
            audio_pat = rearrange(audio_pat, 'n (b t) d -> b t n d', t=self.spec_frames)
            audio_pat = audio_pat + self.audio_space_pos
            audio_pat = rearrange(audio_pat, 'b t n d -> b n t d')
            audio_pat = audio_pat + self.audio_temporal_pos
            audio_pat = rearrange(audio_pat, 'b n t d -> b (n t) d')
        
        q = F.linear(input=audio_pat, weight=self.q.weight, bias=self.q_bias)
        q = rearrange(q, 'b n (h d) -> b h n d', h=self.num_head)
        kv = F.linear(input=s_x_pat, weight=self.kv.weight, bias=self.kv_bias)
        kv = rearrange(kv, 'b n (e h d) -> e b h n d',e=2, h=self.num_head)
        k, v = kv[0], kv[1]
        
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))
        
        attn = attn.softmax(dim=-1)
        
        audio_pat = (attn @ v)
        audio_pat = rearrange(audio_pat, 'b h n d -> b n (h d)')
        audio_pat = self.proj(audio_pat)
        if not self.attn_all_frame:
            audio_pat = rearrange(audio_pat, 'b n d -> n b d')
        else:
            audio_pat = rearrange(audio_pat, 'b (n t) d -> n (b t) d', t=self.spec_frames)
        audio = audio_pat
        return audio
    
    def forward(self, s_x: torch.Tensor, audio: torch.Tensor):
        return self.s2audio_cross_attn(s_x, audio)
    
# Audio to spatial attention module.
class CrossAttentionAudio2S(nn.Module):
    def __init__(self, dim: int, audio_dim: int, n_head: int, num_frames: int, spec_frames: int, attn_all_frame=False, audio_patch = 196, attn_mask: torch.Tensor = None):
        super().__init__()
        
        # add for cross-attn
        self.num_frames = num_frames//2
        self.spec_frames = spec_frames
        self.num_head = n_head
        head_dim = dim // self.num_head
        self.scale = head_dim ** -0.5
        all_head_dim = head_dim * self.num_head
        self.attn_all_frame = attn_all_frame
        self.audio_patch = audio_patch
        if not attn_all_frame:
            self.clip_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
            self.audio_space_pos = nn.Parameter(self.scale * torch.randn((audio_patch, dim)))
        else:
            # self.clip_st_pos = nn.Parameter(self.scale * torch.randn((196 * num_frames//2, dim)))
            # self.audio_st_pos = nn.Parameter(self.scale * torch.randn((audio_patch * spec_frames, dim)))
            self.clip_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
            self.audio_space_pos = nn.Parameter(self.scale * torch.randn((audio_patch, dim)))
            self.clip_temporal_pos = nn.Parameter(self.scale * torch.randn((num_frames//2, dim)))
            self.audio_temporal_pos = nn.Parameter(self.scale * torch.randn((spec_frames, dim)))
            
        self.q = nn.Linear(dim, all_head_dim, bias=False)
        self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
        self.kv = nn.Linear(audio_dim, all_head_dim * 2, bias=False) # 197 tokens(cls+patch) * num_frames
        self.kv_bias = nn.Parameter(torch.zeros(all_head_dim * 2))
        
        self.proj = nn.Linear(all_head_dim, dim)
    
    def audio2s_cross_attn(self, s_x, audio): # s_x=[n (b t) d], t_x=[b (t n) d], text=[m=77 b d]
        t = self.num_frames
        s_x_cls, s_x_pat = s_x[:1,:,:], s_x[1:, :, :]
        audio_pat = audio
        if not self.attn_all_frame:
            s_x_pat = rearrange(s_x_pat, 'n b d -> b n d') # batch -> token
            s_x_pat = s_x_pat + self.clip_space_pos
            audio_pat = rearrange(audio_pat, 'n b d -> b n d') # batch -> token
            audio_pat = audio_pat + self.audio_space_pos
        else:
            # s_x_pat = rearrange(s_x_pat, 'n (b t) d -> b (n t) d', t=t) # batch -> token
            # s_x_pat = s_x_pat + self.clip_st_pos
            # audio_pat = rearrange(audio_pat, 'n (b t) d -> b (n t) d', t=self.spec_frames) # batch -> token
            # audio_pat = audio_pat + self.audio_st_pos
            s_x_pat = rearrange(s_x_pat, 'n (b t) d -> b t n d', t=t)
            s_x_pat = s_x_pat + self.clip_space_pos
            s_x_pat = rearrange(s_x_pat, 'b t n d -> b n t d')
            s_x_pat = s_x_pat + self.clip_temporal_pos
            s_x_pat = rearrange(s_x_pat, 'b n t d -> b (n t) d')
            
            audio_pat = rearrange(audio_pat, 'n (b t) d -> b t n d', t=self.spec_frames)
            audio_pat = audio_pat + self.audio_space_pos
            audio_pat = rearrange(audio_pat, 'b t n d -> b n t d')
            audio_pat = audio_pat + self.audio_temporal_pos
            audio_pat = rearrange(audio_pat, 'b n t d -> b (n t) d')
        
        q = F.linear(input=s_x_pat, weight=self.q.weight, bias=self.q_bias)
        q = rearrange(q, 'b n (h d) -> b h n d', h=self.num_head)
        kv = F.linear(input=audio_pat, weight=self.kv.weight, bias=self.kv_bias)
        kv = rearrange(kv, 'b m (e h d) -> e b h m d',e=2, h=self.num_head)
        k, v = kv[0], kv[1]
        
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))
        
        attn = attn.softmax(dim=-1)
        
        x_pat = (attn @ v)
        x_pat = rearrange(x_pat, 'b h n d -> b n (h d)')
        x_pat = self.proj(x_pat)
        if not self.attn_all_frame:
            x_pat = rearrange(x_pat, 'b n d -> n b d')
        else:
            x_pat = rearrange(x_pat, 'b (n t) d -> n (b t) d', t=t)
        s_x = torch.cat([s_x_cls, x_pat], dim=0)
        return s_x

    def forward(self, s_x: torch.Tensor, audio: torch.Tensor):
        return self.audio2s_cross_attn(s_x, audio)
    
# temporal to Audio attention module.
class CrossAttentionT2Audio(nn.Module):
    def __init__(self, dim: int, audio_dim: int, n_head: int, num_frames: int, spec_frames: int, attn_all_frame = False, audio_patch = 196, attn_mask: torch.Tensor = None):
        super().__init__()
        
        # add for cross-attn
        self.num_frames = num_frames//2
        self.spec_frames = spec_frames
        self.num_head = n_head
        head_dim = audio_dim // self.num_head
        self.scale = head_dim ** -0.5
        all_head_dim = head_dim * self.num_head
        self.attn_all_frame = attn_all_frame
        self.audio_patch = audio_patch
        if not attn_all_frame:
            self.vmae_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
            self.audio_space_pos = nn.Parameter(self.scale * torch.randn((audio_patch, dim)))
        else:
            # self.vmae_st_pos = nn.Parameter(self.scale * torch.randn((196 * num_frames//2, dim)))
            # self.audio_st_pos = nn.Parameter(self.scale * torch.randn((audio_patch * spec_frames, dim)))
            self.vmae_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
            self.audio_space_pos = nn.Parameter(self.scale * torch.randn((audio_patch, dim)))
            self.vmae_temporal_pos = nn.Parameter(self.scale * torch.randn((num_frames//2, dim)))
            self.audio_temporal_pos = nn.Parameter(self.scale * torch.randn((spec_frames, dim)))
        
        self.q = nn.Linear(audio_dim, all_head_dim, bias=False)
        self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
        self.kv = nn.Linear(dim, all_head_dim * 2, bias=False) # 197 tokens(cls+patch) * num_frames
        self.kv_bias = nn.Parameter(torch.zeros(all_head_dim * 2))
        
        self.proj = nn.Linear(all_head_dim, audio_dim)
    
    def t2audio_cross_attn(self, t_x, audio): # s_x=[n (b t) d], t_x=[b (t n) d], text=[m=77 b d]
        t = self.num_frames
        n = t_x.shape[1] // t
        audio_pat = audio
        if not self.attn_all_frame:
            t_x = rearrange(t_x, 'b (t n) d -> (b t) n d', t=t)
            t_x = t_x + self.vmae_space_pos
            audio_pat = rearrange(audio_pat, 'n b d -> b n d') # batch -> token
            audio_pat = audio_pat + self.audio_space_pos
        else:
            # t_x = t_x + self.vmae_st_pos
            # audio_pat = rearrange(audio_pat, 'n (b t) d -> b (n t) d', t=self.spec_frames) # batch -> token
            # audio_pat = audio_pat + self.audio_st_pos
            t_x = rearrange(t_x, 'b (t n) d -> b t n d', t=t)
            t_x = t_x + self.vmae_space_pos
            t_x = rearrange(t_x, 'b t n d -> b n t d')
            t_x = t_x + self.vmae_temporal_pos
            t_x = rearrange(t_x, 'b n t d -> b (t n) d')
            
            audio_pat = rearrange(audio_pat, 'n (b t) d -> b t n d', t=self.spec_frames)
            audio_pat = audio_pat + self.audio_space_pos
            audio_pat = rearrange(audio_pat, 'b t n d -> b n t d')
            audio_pat = audio_pat + self.audio_temporal_pos
            audio_pat = rearrange(audio_pat, 'b n t d -> b (n t) d')
        
        q = F.linear(input=audio_pat, weight=self.q.weight, bias=self.q_bias)
        q = rearrange(q, 'b n (h d) -> b h n d', h=self.num_head)
        kv = F.linear(input=t_x, weight=self.kv.weight, bias=self.kv_bias)
        kv = rearrange(kv, 'b n (e h d) -> e b h n d',e=2, h=self.num_head)
        k, v = kv[0], kv[1]
        
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))
        
        attn = attn.softmax(dim=-1)
        
        audio_pat = (attn @ v)
        audio_pat = rearrange(audio_pat, 'b h n d -> b n (h d)')
        audio_pat = self.proj(audio_pat)
        if not self.attn_all_frame:
            audio_pat = rearrange(audio_pat, 'b n d -> n b d')
        else:
            audio_pat = rearrange(audio_pat, 'b (n t) d -> n (b t) d', t=self.spec_frames)
        audio = audio_pat
        return audio

    def forward(self, t_x: torch.Tensor, audio: torch.Tensor,):
        return self.t2audio_cross_attn(t_x, audio)
    
# Audio to temporal cross attention module.
class CrossAttentionAudio2T(nn.Module):
    def __init__(self, dim: int, audio_dim: int, n_head: int, num_frames: int, spec_frames: int, attn_all_frame = False, audio_patch = 196, attn_mask: torch.Tensor = None):
        super().__init__()

        # add for cross-attn
        self.num_frames = num_frames//2
        self.spec_frames = spec_frames
        self.num_head = n_head
        head_dim = dim // self.num_head
        self.scale = head_dim ** -0.5
        all_head_dim = head_dim * self.num_head
        self.attn_all_frame = attn_all_frame
        self.audio_patch = audio_patch
        if not attn_all_frame:
            self.vmae_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
            self.audio_space_pos = nn.Parameter(self.scale * torch.randn((audio_patch, dim)))
        else:
            # self.vmae_st_pos = nn.Parameter(self.scale * torch.randn((196 * num_frames//2, dim)))
            # self.audio_st_pos = nn.Parameter(self.scale * torch.randn((audio_patch * spec_frames, dim)))
            self.vmae_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
            self.audio_space_pos = nn.Parameter(self.scale * torch.randn((audio_patch, dim)))
            self.vmae_temporal_pos = nn.Parameter(self.scale * torch.randn((num_frames//2, dim)))
            self.audio_temporal_pos = nn.Parameter(self.scale * torch.randn((spec_frames, dim)))
        
        self.q = nn.Linear(dim, all_head_dim, bias=False)
        self.q_bias = nn.Parameter(torch.zeros(all_head_dim))
        self.kv = nn.Linear(audio_dim, all_head_dim * 2, bias=False) # 197 tokens(cls+patch) * num_frames
        self.kv_bias = nn.Parameter(torch.zeros(all_head_dim * 2))
        
        self.proj = nn.Linear(all_head_dim, dim)
    
    def audio2t_cross_attn(self, t_x, audio): # s_x=[n (b t) d], t_x=[b (t n) d], text=[m=77 b d]
        t = self.num_frames
        n = t_x.shape[1] // t
        audio_pat = audio
        if not self.attn_all_frame:
            t_x = rearrange(t_x, 'b (t n) d -> (b t) n d', t=t)
            t_x = t_x + self.vmae_space_pos
            audio_pat = rearrange(audio_pat, 'n b d -> b n d') # batch -> token
            audio_pat = audio_pat + self.audio_space_pos
        else:
            # t_x = t_x + self.vmae_st_pos
            # audio_pat = rearrange(audio_pat, 'n (b t) d -> b (n t) d', t=self.spec_frames) # batch -> token
            # audio_pat = audio_pat + self.audio_st_pos
            t_x = rearrange(t_x, 'b (t n) d -> b t n d', t=t)
            t_x = t_x + self.vmae_space_pos
            t_x = rearrange(t_x, 'b t n d -> b n t d')
            t_x = t_x + self.vmae_temporal_pos
            t_x = rearrange(t_x, 'b n t d -> b (t n) d')
            
            audio_pat = rearrange(audio_pat, 'n (b t) d -> b t n d', t=self.spec_frames)
            audio_pat = audio_pat + self.audio_space_pos
            audio_pat = rearrange(audio_pat, 'b t n d -> b n t d')
            audio_pat = audio_pat + self.audio_temporal_pos
            audio_pat = rearrange(audio_pat, 'b n t d -> b (n t) d')
        
        q = F.linear(input=t_x, weight=self.q.weight, bias=self.q_bias)
        q = rearrange(q, 'b n (h d) -> b h n d', h=self.num_head)
        kv = F.linear(input=audio_pat, weight=self.kv.weight, bias=self.kv_bias)
        kv = rearrange(kv, 'b n (e h d) -> e b h n d',e=2, h=self.num_head)
        k, v = kv[0], kv[1]
        
        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))
        
        attn = attn.softmax(dim=-1)
        
        t_x = (attn @ v)
        t_x = rearrange(t_x, 'b h t d -> b t (h d)')
        t_x = self.proj(t_x)
        if not self.attn_all_frame:
            t_x = rearrange(t_x, '(b t) n d -> b (t n) d', t=t)
        return t_x

    def forward(self, t_x: torch.Tensor, audio: torch.Tensor,):
        return self.audio2t_cross_attn(t_x, audio)

# spatial to temporal cross attention module.
class CrossAttentionS2T(nn.Module):
    def __init__(self, dim: int, n_head: int, num_frames: int, attn_mask: torch.Tensor = None):
        super().__init__()

        # add for cross-attn
        self.num_frames = num_frames
        self.num_head = n_head
        head_dim = dim // self.num_head
        self.scale = head_dim ** -0.5
        all_head_dim = head_dim * self.num_head
        self.clip_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
        self.vmae_space_pos = nn.Parameter(self.scale * torch.randn((196, dim)))
        

        self.s2t_q = nn.Linear(dim, all_head_dim, bias=False)
        self.s2t_q_bias = nn.Parameter(torch.zeros(all_head_dim))
        self.s2t_kv = nn.Linear(dim, all_head_dim * 2, bias=False) # 197 tokens(cls+patch) * num_frames
        self.s2t_kv_bias = nn.Parameter(torch.zeros(all_head_dim * 2))
        
        self.t2s_proj = nn.Linear(all_head_dim, dim)
        
        self.attn_mask = attn_mask
    
    def s2t_cross_attn(self, s_x, t_x): # s_x=[n (b t) d], t_x=[b (t n) d]
        B, _, _ = t_x.shape
        t = s_x.shape[1] // t_x.shape[0]
        s_x_pat = s_x[1:, :, :]
        s_x_pat = rearrange(s_x_pat, 'n b d -> b n d') # batch -> token
        s_x_pat = s_x_pat + self.clip_space_pos
        t_x = rearrange(t_x, 'b (t n) d -> (b t) n d', t=t)
        t_x = t_x + self.vmae_space_pos
        s2t_q_bias = self.s2t_q_bias
        s2t_kv_bias = self.s2t_kv_bias
        
        s2t_q = F.linear(input=t_x, weight=self.s2t_q.weight, bias=s2t_q_bias)
        s2t_q = rearrange(s2t_q, 'b n (h d) -> b h n d', h=self.num_head)
        s2t_kv = F.linear(input=s_x_pat, weight=self.s2t_kv.weight, bias=s2t_kv_bias)
        s2t_kv = rearrange(s2t_kv, 'b n (e h d) -> e b h n d',e=2, h=self.num_head)
        s2t_k, s2t_v = s2t_kv[0], s2t_kv[1]
        
        s2t_q = s2t_q * self.scale
        s2t_attn = (s2t_q @ s2t_k.transpose(-2, -1))
        
        s2t_attn = s2t_attn.softmax(dim=-1)
        
        t_x = (s2t_attn @ s2t_v)
        t_x = rearrange(t_x, 'b h t d -> b t (h d)')
        t_x = self.t2s_proj(t_x)
        t_x = rearrange(t_x, '(b t) n d -> b (t n) d', b=B)
        return t_x

    def forward(self, s_x: torch.Tensor, t_x: torch.Tensor):
        return self.s2t_cross_attn(s_x, t_x)


# this codes from CLIP github(https://github.com/openai/CLIP)
class CrossAttentionT2S(nn.Module):
    def __init__(self, dim: int, n_head: int, num_frames: int, attn_mask: torch.Tensor = None):
        super().__init__()

        self.num_frames = num_frames
        self.num_head = n_head
        head_dim = dim // self.num_head
        self.scale = head_dim ** -0.5
        all_head_dim = head_dim * self.num_head
        self.clip_time_pos = nn.Parameter(self.scale * torch.randn((num_frames//2, dim)))
        self.vmae_time_pos = nn.Parameter(self.scale * torch.randn((num_frames//2, dim)))
        
        self.t2s_q = nn.Linear(dim, all_head_dim, bias=False) # 197 tokens(cls+patch) * num_frames
        self.t2s_q_bias = nn.Parameter(torch.zeros(all_head_dim))
        self.t2s_kv = nn.Linear(dim, all_head_dim * 2, bias=False)
        self.t2s_kv_bias = nn.Parameter(torch.zeros(all_head_dim * 2))
        
        self.t2s_proj = nn.Linear(all_head_dim, dim)
        
        self.attn_mask = attn_mask
    
    def t2s_cross_attn(self, s_x, t_x): # s_x=[n (b t) d], t_x=[b n d]
        B, _, _ = t_x.shape
        t = s_x.shape[1] // t_x.shape[0]
        s_x_cls, s_x_pat = s_x[0, :, :], s_x[1:, :, :]
        s_x_pat = rearrange(s_x_pat, 'n (b t) d -> (b n) t d', b=B) # batch -> token
        s_x_pat = s_x_pat + self.clip_time_pos
        t_x = rearrange(t_x, 'b (t n) d -> (b n) t d', t=t)
        t_x = t_x + self.vmae_time_pos
        t2s_q_bias = self.t2s_q_bias
        t2s_kv_bias = self.t2s_kv_bias
        
        t2s_q = F.linear(input=s_x_pat, weight=self.t2s_q.weight, bias=t2s_q_bias)
        t2s_q = rearrange(t2s_q, 'b t (h d) -> b h t d', h=self.num_head)
        t2s_kv = F.linear(input=t_x, weight=self.t2s_kv.weight, bias=t2s_kv_bias)
        t2s_kv = rearrange(t2s_kv, 'b t (e h d) -> e b h t d',e=2, h=self.num_head)
        t2s_k, t2s_v = t2s_kv[0], t2s_kv[1]
        
        t2s_q = t2s_q * self.scale
        t2s_attn = (t2s_q @ t2s_k.transpose(-2, -1))
        
        t2s_attn = t2s_attn.softmax(dim=-1)
        
        s_x_pat = (t2s_attn @ t2s_v)
        s_x_pat = rearrange(s_x_pat, 'b h n d -> b n (h d)')
        s_x_pat = self.t2s_proj(s_x_pat)
        s_x_pat = rearrange(s_x_pat,'(b n) t d -> n (b t) d', b=B)
        s_x = torch.cat([s_x_cls.unsqueeze(0), s_x_pat], dim=0)
        return s_x

    def forward(self, s_x: torch.Tensor, t_x: torch.Tensor):
        return self.t2s_cross_attn(s_x, t_x)

class B_CAST(nn.Module):
    def __init__(self, dim, num_heads, num_frames=16, down_ratio=2, text_dim=512, text_num_heads=8, 
                 drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, type='s-text', attn_all_frame=True, spec_frames=8, audio_patch=196):
        super().__init__()
        self.num_frames = num_frames
        self.down_ratio = down_ratio
        self.down_ratio = down_ratio
        self.act = act_layer()
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        
        self.cross_l_down = nn.Linear(dim, dim//self.down_ratio)
        self.ln_l_cross = norm_layer(dim//self.down_ratio)
        if type == 's-t':
            self.cross_r_down = nn.Linear(dim, dim//self.down_ratio)
            self.ln_r_cross = norm_layer(dim//self.down_ratio)
            self.l2r_cross = CrossAttentionS2T(dim//self.down_ratio, num_heads, num_frames)
            self.r2l_cross = CrossAttentionT2S(dim//self.down_ratio, num_heads, num_frames)
            self.cross_r_up = nn.Linear(dim//self.down_ratio, dim)
        elif type == 's-audio':
            self.cross_r_down = nn.Linear(text_dim, text_dim//self.down_ratio)
            self.ln_r_cross = norm_layer(text_dim//self.down_ratio)
            self.l2r_cross = CrossAttentionS2Audio(dim//self.down_ratio, text_dim//self.down_ratio, text_num_heads, num_frames, spec_frames, attn_all_frame, audio_patch)
            self.r2l_cross = CrossAttentionAudio2S(dim//self.down_ratio, text_dim//self.down_ratio, text_num_heads, num_frames, spec_frames, attn_all_frame, audio_patch)
            self.cross_r_up = nn.Linear(text_dim//self.down_ratio, text_dim)
        elif type == 't-audio':
            self.cross_r_down = nn.Linear(text_dim, text_dim//self.down_ratio)
            self.ln_r_cross = norm_layer(text_dim//self.down_ratio)
            self.l2r_cross = CrossAttentionT2Audio(dim//self.down_ratio, text_dim//self.down_ratio, text_num_heads, num_frames, spec_frames, attn_all_frame, audio_patch)
            self.r2l_cross = CrossAttentionAudio2T(dim//self.down_ratio, text_dim//self.down_ratio, text_num_heads, num_frames, spec_frames, attn_all_frame, audio_patch)
            self.cross_r_up = nn.Linear(text_dim//self.down_ratio, text_dim)
        self.cross_l_up = nn.Linear(dim//self.down_ratio, dim)
            
    def forward(self, l, r):
        n_l = self.ln_l_cross(self.cross_l_down(l))
        n_r = self.ln_r_cross(self.cross_r_down(r))
        c_l = self.cross_l_up(self.act(self.r2l_cross(n_l, n_r)))
        c_r = self.cross_r_up(self.act(self.l2r_cross(n_l, n_r)))
        l = l + self.drop_path(c_l)
        r = r + self.drop_path(c_r)
        return l, r
    
class Block(nn.Module):
    def __init__(self, dim, num_heads, num_frames=16, mlp_ratio=4., down_ratio=2, qkv_bias=False, qk_scale=None, drop=0., attn_drop=0.,
                 drop_path=0., init_values=None, num_layer=0, act_layer=nn.GELU, norm_layer=nn.LayerNorm, attn_head_dim=None,
                 text_dim=512, text_num_heads=8, use_Adapter=False, CA=[i for i in range(12)], 
                 spec_frames=8, attn_all_frame=True, audio_patch=196, CA_eq=False, relative_position_embedding=True, num_buckets=320, max_distance=800, gru_rel_pos=True):
        super().__init__()
        self.num_layer = num_layer
        self.CA = CA
        self.CA_eq = CA_eq
        self.num_heads = num_heads
        self.down_ratio = down_ratio
        self.scale = 0.5
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.act = act_layer()
        self.use_Adapter = use_Adapter
        self.spec_frames = spec_frames
        self.num_frames = num_frames//2
        self.audio_patch = audio_patch
        
        ###################################### MHSA code #####################################
        ############################ AIM MHSA ###########################
        self.clip_ln_1 = LayerNorm(dim)
        self.clip_attn = nn.MultiheadAttention(dim, num_heads)
        if self.use_Adapter:
            self.S_Adapter = Adapter(dim)
        ##################################################################
        
        ############################ VMAE MHSA ###########################
        self.norm1 = norm_layer(dim)
        self.attn = Attention(
            dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
            attn_drop=attn_drop, proj_drop=drop, attn_head_dim=attn_head_dim)
        if self.use_Adapter:
            self.T_Adapter = Adapter(dim)
        ##################################################################
        
        ############################ BEATs MHSA ######################
        self.activation_fn = get_activation_fn('gelu')
        self.self_attn = MultiheadAttention(
            dim,
            num_heads,
            dropout=attn_drop,
            self_attention=True,
            has_relative_attention_bias=relative_position_embedding,
            num_buckets=num_buckets,
            max_distance=max_distance,
            rescale_init=False,
            gru_rel_pos=gru_rel_pos,
        )
        self.dropout1 = nn.Dropout(drop)
        activation_dropout = 0.0
        self.dropout2 = nn.Dropout(activation_dropout)
        self.dropout3 = nn.Dropout(drop)
        self.self_attn_layer_norm = LayerNorm(dim)
        if self.use_Adapter:
            self.Text_Adapter = Adapter(text_dim)
        ##################################################################
        #########################################################################################
        
        ###################################### Cross attention ####################################
        if not self.CA_eq or self.num_layer in self.CA:
            self.s_t_b_cast = B_CAST(dim, num_heads, num_frames, down_ratio, text_dim, text_num_heads, drop_path, act_layer, norm_layer, type='s-t')
        if self.num_layer in self.CA:
            self.s_text_b_cast = B_CAST(dim, num_heads, num_frames, down_ratio, text_dim, text_num_heads, drop_path, act_layer, norm_layer, type='s-audio', spec_frames=spec_frames, attn_all_frame=attn_all_frame, audio_patch=audio_patch)
            self.t_text_b_cast = B_CAST(dim, num_heads, num_frames, down_ratio, text_dim, text_num_heads, drop_path, act_layer, norm_layer, type='t-audio', spec_frames=spec_frames, attn_all_frame=attn_all_frame, audio_patch=audio_patch)
            
        ###########################################################################################
        
        ###################################### FFN code #########################################
        ############################ AIM FFN ###############################
        self.clip_ln_2 = LayerNorm(dim)
        self.clip_mlp = nn.Sequential(OrderedDict([
            ("c_fc", nn.Linear(dim, dim * 4)),
            ("gelu", QuickGELU()),
            ("c_proj", nn.Linear(dim * 4, dim))
        ]))
        if self.use_Adapter:
            self.S_MLP_Adapter = Adapter(dim, skip_connect=False)
        self.attn_mask = None
        #####################################################################
        
        ############################ VMAE FFN ###############################
        self.norm2 = norm_layer(dim)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        if self.use_Adapter:
            self.T_MLP_Adapter = Adapter(dim, skip_connect=False)
        #####################################################################
        
        ############################ BEATs FFN ###############################
        self.fc1 = nn.Linear(dim, mlp_hidden_dim)
        self.fc2 = nn.Linear(mlp_hidden_dim, dim)
        self.final_layer_norm = LayerNorm(dim)
        self.deep_norm_alpha = math.pow(2 * num_layer, 1 / 4)
        if self.use_Adapter:
            self.Text_MLP_Adapter = Adapter(text_dim, skip_connect=False)
        self.attn_mask = None
        #####################################################################
        
        #########################################################################################
        

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        
    def attention(self, x: torch.Tensor):
        self.attn_mask = self.attn_mask.to(dtype=x.dtype, device=x.device) if self.attn_mask is not None else None
        return self.clip_attn(x, x, x, need_weights=False, attn_mask=self.attn_mask)[0]

    def forward(self,s_x, t_x, text, pos_bias=None):
        B = t_x.shape[0]
        n, bt, _ = s_x.shape
        num_frames = bt//B
        
        ############################ MHSA Forward #############################
        residual = text
        text, attn, pos_bias = self.self_attn(
            query=text,
            key=text,
            value=text,
            key_padding_mask=torch.zeros(B, self.audio_patch, device=t_x.device).bool(),
            need_weights=False,
            attn_mask=None,
            position_bias=pos_bias
        )
        text = self.dropout1(text)
        if self.use_Adapter:
            # AIM Space MHSA
            s_x = s_x + self.S_Adapter(self.attention(self.clip_ln_1(s_x)))
            # VMAE Time MHSA
            t_x = t_x + self.T_Adapter(self.attn(self.norm1(t_x)))
        else:
            # AIM Space MHSA
            s_x = s_x + self.attention(self.clip_ln_1(s_x))
            # VMAE Time MHSA
            t_x = t_x + self.attn(self.norm1(t_x))
        # BEATs Space MHSA
        if self.use_Adapter:
            text = residual * self.deep_norm_alpha + self.Text_Adapter(text)
            text = self.self_attn_layer_norm(text)
        else:
            text = residual * self.deep_norm_alpha + text
            text = self.self_attn_layer_norm(text)
        ########################################################################
        
        ############################ Cross Forward #############################
        if not self.CA_eq or self.num_layer in self.CA:
            s_x, t_x = self.s_t_b_cast(s_x, t_x)
        if self.num_layer in self.CA:
            s_x, text = self.s_text_b_cast(s_x, text)
            t_x, text = self.t_text_b_cast(t_x, text)
        #########################################################################
        
        ############################ FFN Forward ##################################
        s_xn = self.clip_ln_2(s_x)
        t_xn = self.norm2(t_x)
        if self.use_Adapter:
            s_x = s_x + self.clip_mlp(s_xn) + self.drop_path(self.scale * self.S_MLP_Adapter(s_xn))
            t_x = t_x + self.mlp(t_xn) + self.drop_path(self.scale * self.T_MLP_Adapter(t_xn))
        else:
            s_x = s_x + self.clip_mlp(s_xn)
            t_x = t_x + self.mlp(t_xn)
        residual = text
        text = self.activation_fn(self.fc1(text))
        text = self.dropout2(text)
        text = self.fc2(text)
        text = self.dropout3(text)
        if self.use_Adapter:
            text = residual * self.deep_norm_alpha + text + self.drop_path(self.scale * self.Text_MLP_Adapter(residual))
            text = self.final_layer_norm(text)
        else:
            text = residual * self.deep_norm_alpha + text
            text = self.final_layer_norm(text)
        ############################################################################
        
        return s_x, t_x, text, pos_bias

class STCrossTransformer(nn.Module):
    """ Vision Transformer with support for patch or hybrid CNN input stage
    """
    def __init__(self, 
                 img_size=224, 
                 patch_size=16, 
                 in_chans=3, 
                 num_classes=1000, 
                 embed_dim=768,
                 text_dim=512, 
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
                 pretrained_cfg = None,
                 pretrained_cfg_overlay = None,
                 text_num_heads = 8,
                 vocab_size = 49408,
                 context_length = 77,
                 audio_enabled = False,
                 prefix = None,
                 postfix = None,
                 CA = 9,
                 output_text_dim = 512,
                 spec_frames=16,
                 attn_all_frame=True,
                 audio_patch=196,
                 CA_eq=False,
                 use_Adapter=True,
                 use_textF = True):
        super().__init__()
        self.num_classes = num_classes
        self.num_frames = all_frames
        self.embed_dim = embed_dim  # num_features for consistency with other models
        self.tubelet_size = tubelet_size
        self.down_ratio = down_ratio
        self.composition = composition
        # ==============================================================================================================
        self.text_dim = text_dim
        self.prefix = prefix
        self.postfix = postfix
        spec_frames = (spec_frames+1) // 2
        self.audio_patch = audio_patch
            
        self.embed = 512
        input_patch_size = 16
        self.patch_embedding = nn.Conv2d(1, self.embed, kernel_size=input_patch_size, stride=input_patch_size, bias=False)
        self.layer_norm = LayerNorm(self.embed)
        
        self.post_extract_proj = (
            nn.Linear(self.embed, self.embed_dim)
            if self.embed != self.embed_dim
            else None
        )
        self.dropout_input = nn.Dropout(0.0)
        
        # self.layer_wise_gradient_decay_ratio = 0.6
        conv_pos = 128
        self.pos_conv = nn.Conv1d(
            self.embed_dim,
            self.embed_dim,
            kernel_size=conv_pos, # args.conv_pos
            padding=conv_pos // 2, # args.conv_pos // 2
            groups=16, # args.conv_pos_groups
        )
        self.pos_conv = nn.utils.weight_norm(self.pos_conv, name="weight", dim=2)
        self.pos_conv = nn.Sequential(self.pos_conv, SamePad(conv_pos), nn.GELU())
        self.layer_norm_first = LayerNorm(self.embed_dim)
        self.layerdrop = 0.05
        
        self.relative_position_embedding = True # args.relative_position_embedding
        self.num_buckets = 320 # args.num_buckets
        self.max_distance = 800 # args.max_distance
        gru_rel_pos = True
        
        
        # abliation study 용
        self.split_projection = False
        self.use_videoF = True
        self.use_textF = True
        CA = [i for i in range(CA, 12)]
        attn_all_frame = attn_all_frame
        # ==============================================================================================================
        
        self.patch_embed = PatchEmbed(
            img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim, num_frames=all_frames, tubelet_size=self.tubelet_size)
        num_patches = self.patch_embed.num_patches
        
        scale = embed_dim ** -0.5
        self.clip_conv1 = nn.Conv2d(in_channels=3, out_channels=embed_dim, kernel_size=patch_size, stride=patch_size, bias=False)
        self.clip_class_embedding = nn.Parameter(scale * torch.randn(embed_dim))
        self.clip_positional_embedding = nn.Parameter(scale * torch.randn((img_size // patch_size) ** 2 + 1, embed_dim))
        self.clip_ln_pre = LayerNorm(embed_dim)

        if use_learnable_pos_emb:
            self.pos_embed = nn.Parameter(torch.zeros(1, num_patches, embed_dim))
        else:
            # sine-cosine positional embeddings is on the way
            self.pos_embed = get_sinusoid_encoding_table(num_patches, embed_dim)

        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, num_frames=self.num_frames, mlp_ratio=mlp_ratio,down_ratio=self.down_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                init_values=init_values, num_layer=i, text_dim=self.text_dim, text_num_heads=text_num_heads, use_Adapter=use_Adapter, CA=CA,
                spec_frames=spec_frames, attn_all_frame=attn_all_frame, audio_patch=audio_patch, CA_eq=CA_eq,
                relative_position_embedding=self.relative_position_embedding, num_buckets=self.num_buckets, max_distance=self.max_distance, gru_rel_pos=gru_rel_pos)
            for i in range(depth)])
        
        self.clip_ln_post = LayerNorm(embed_dim)
        self.vmae_fc_norm = norm_layer(embed_dim)
        
        # 768 to 512
        if self.composition:
            if self.use_videoF:
                self.noun_last_Adapter = Adapter(embed_dim, skip_connect=False)
                self.verb_last_Adapter = Adapter(embed_dim, skip_connect=False)
            if self.use_textF:
                self.text_noun_last_Adapter = nn.Sequential(OrderedDict([
                    ("c_fc", nn.Linear(output_text_dim, text_dim // 4)),
                    ("gelu", QuickGELU()),
                    ("c_proj", nn.Linear(text_dim // 4, embed_dim))
                ]))
                self.text_verb_last_Adapter = nn.Sequential(OrderedDict([
                    ("c_fc", nn.Linear(output_text_dim, text_dim // 4)),
                    ("gelu", QuickGELU()),
                    ("c_proj", nn.Linear(text_dim // 4, embed_dim))
                ]))
            self.head_verb = nn.Linear(embed_dim, 97)
            self.head_verb_dropout = nn.Dropout(head_drop_rate)
            self.head_noun = nn.Linear(embed_dim, 300)
            self.head_noun_dropout = nn.Dropout(head_drop_rate)
            pass
        else:
            if self.use_videoF:
                self.noun_last_Adapter = Adapter(embed_dim, skip_connect=False)
                self.verb_last_Adapter = Adapter(embed_dim, skip_connect=False)
            if self.use_textF:
                self.text_verb_last_Adapter = nn.Sequential(OrderedDict([
                    ("c_fc", nn.Linear(output_text_dim, text_dim // 4)),
                    ("gelu", QuickGELU()),
                    ("c_proj", nn.Linear(text_dim // 4, embed_dim))
                ]))
            self.head = nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()
            self.head_dropout = nn.Dropout(head_drop_rate)

        if use_learnable_pos_emb:
            trunc_normal_(self.pos_embed, std=.02)

        self.apply(self._init_weights)
        self._init_adpater_weight()
        
        if self.composition:
            if self.use_videoF:
                nn.init.constant_(self.noun_last_Adapter.D_fc2.weight, 0)
                nn.init.constant_(self.verb_last_Adapter.D_fc2.weight, 0)
            self.head_verb.weight.data.mul_(init_scale)
            self.head_verb.bias.data.mul_(init_scale)
            self.head_noun.weight.data.mul_(init_scale)
            self.head_noun.bias.data.mul_(init_scale)
            pass
        else:
            nn.init.constant_(self.noun_last_Adapter.D_fc2.weight, 0)
            nn.init.constant_(self.verb_last_Adapter.D_fc2.weight, 0)
            self.head.weight.data.mul_(init_scale)
            self.head.bias.data.mul_(init_scale)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)
            
    def _init_adpater_weight(self):
        for n, m in self.blocks.named_modules():
            if 'Adapter' in n:
                for n2, m2 in m.named_modules():
                    if 'D_fc2' in n2:
                        if isinstance(m2, nn.Linear):
                            nn.init.constant_(m2.weight, 0)
                            nn.init.constant_(m2.bias, 0)
            elif 'up' in n:
                for n2, m2 in m.named_modules():
                    if isinstance(m2, nn.Linear):
                        nn.init.constant_(m2.weight, 0)
                        nn.init.constant_(m2.bias, 0)

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
    
    def reset_fcnorm(self):
        self.vmae_fc_norm = nn.LayerNorm(self.embed_dim)

    def forward_features(self, x, spec=None, caption=None, split_projection=False):
        B = x.shape[0]
        s_x = x[:, :, 1::2, :, :] # pick even frames
        ######################## AIM spatial path #########################
        s_t = s_x.shape[2]
        s_x = rearrange(s_x, 'b c t h w -> (b t) c h w')
        s_x = self.clip_conv1(s_x) # shape = [*, embeddim, grid, grid]
        s_x = s_x.reshape(s_x.shape[0], s_x.shape[1], -1) # [*, embeddim, grid**2]
        s_x = s_x.permute(0, 2, 1) # shape[batch, patchnum, embeddim]
        s_x = torch.cat([self.clip_class_embedding.to(s_x.dtype) + torch.zeros(s_x.shape[0], 1, s_x.shape[-1], dtype=s_x.dtype, device=s_x.device), s_x], dim=1)
        s_x = s_x + self.clip_positional_embedding.to(s_x.dtype)
        s_x = self.clip_ln_pre(s_x)
        #####################################################################
        
        ######################## VMAE spatial path #########################
        t_x = self.patch_embed(x)

        if self.pos_embed is not None:
            t_x = t_x + self.pos_embed.expand(B, -1, -1).type_as(t_x).to(t_x.device).clone().detach()
        t_x = self.pos_drop(t_x)
        #####################################################################
        
        ######################## BEATs path #############################
        spec_x = spec[:, :, 1::2, :, :] if spec.dim() == 5 else spec.unsqueeze(2)
        spec_x = spec_x[:,:1,:,:,:]
        spec_x = rearrange(spec_x, 'b c t h w -> (b t) c h w')
        spec_x = self.patch_embedding(spec_x)
        spec_x = spec_x.reshape(spec_x.shape[0], spec_x.shape[1], -1) # [*, embeddim, grid**2] # B C T
        spec_x = spec_x.permute(0, 2, 1) # shape[batch, patchnum, embeddim] # B T(196) C(768)
        spec_x = self.layer_norm(spec_x)
        
        if self.post_extract_proj is not None:
            spec_x = self.post_extract_proj(spec_x)

        spec_x = self.dropout_input(spec_x)
        #####################################################################
        
        spec_x_conv = self.pos_conv(spec_x.transpose(1, 2))
        spec_x_conv = spec_x_conv.transpose(1, 2)
        spec_x = spec_x + spec_x_conv
        spec_x = self.layer_norm_first(spec_x)
        spec_x = spec_x.transpose(0,1) # T x B x C
        s_x = s_x.permute(1,0,2)
        pos_bias = None
        for blk in self.blocks:
            # s_x, t_x = blk(s_x, t_x, text)
            s_x, t_x, spec_x, pos_bias = blk(s_x, t_x, spec_x, pos_bias)
        s_x = s_x.permute(1,0,2)
        spec_x = spec_x.transpose(0, 1)
        
        spec_x = spec_x.mean(1)
        
        s_x = rearrange(s_x, '(b t) n d -> b t n d', b=B)
        s_x = self.clip_ln_post(s_x[:,:,0,:].mean(1)) # all cls tokens avg pooling
        t_x = self.vmae_fc_norm(t_x.mean(1)) # all patch avg pooling
        return s_x, t_x, spec_x

    def forward(self, x, spec=None, caption=None):
        if self.composition:
            s_x, t_x, text_x = self.forward_features(x, spec=spec)
            if self.use_videoF and self.use_textF:
                s_x = self.noun_last_Adapter(s_x) + self.text_noun_last_Adapter(text_x)
                t_x = self.verb_last_Adapter(t_x) + self.text_verb_last_Adapter(text_x)
            elif self.use_videoF:
                s_x = self.noun_last_Adapter(s_x)
                t_x = self.verb_last_Adapter(t_x)
            else:
                s_x = self.text_noun_last_Adapter(text_x)
                t_x = self.text_verb_last_Adapter(text_x)
            s_x = self.head_noun_dropout(s_x)
            s_x = self.head_noun(s_x)
            t_x = self.head_verb_dropout(t_x)
            t_x = self.head_verb(t_x)
            return s_x, t_x
        else:
            s_x, t_x, text_x = self.forward_features(x, spec=spec)
            if self.use_videoF and self.use_textF:
                x = self.noun_last_Adapter(s_x) + self.verb_last_Adapter(t_x) + self.text_verb_last_Adapter(text_x)
            elif self.use_videoF:
                x = self.noun_last_Adapter(s_x) + self.verb_last_Adapter(t_x)
            else:
                x = self.text_verb_last_Adapter(text_x)
            x = self.head_dropout(x)
            x = self.head(x)
            return x

# audio - single Spec

@register_model
def cast_single_beats_Bsquare_CA9_base_patch16_224(pretrained=False, args=None, class_list=None, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, text_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=False, audio_enabled=False, text_num_heads=12, CA=9, output_text_dim=768,
        prefix = 16, postfix = 16, spec_frames=1, attn_all_frame=True, **kwargs)
    return model

@register_model
def cast_single_beats_Bsquare_CA9_down4_base_patch16_224(pretrained=False, args=None, class_list=None, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, text_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=False, audio_enabled=True, text_num_heads=12, CA=9, output_text_dim=768,
        prefix = 16, postfix = 16, spec_frames=1, attn_all_frame=True, down_ratio=4, **kwargs)
    return model

@register_model
def cast_single_beats_Bsquare_CA0_base_patch16_224(pretrained=False, args=None, class_list=None, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, text_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=True, audio_enabled=False, text_num_heads=12, CA=0, output_text_dim=768,
        prefix = 16, postfix = 16, spec_frames=1, attn_all_frame=True, **kwargs)
    return model

# audio - single Spec ek100

@register_model
def compo_cast_single_beats_Bsquare_CA9_base_patch16_224(pretrained=False, args=None, class_list=None, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, text_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=True, audio_enabled=True, text_num_heads=12, CA=9, output_text_dim=768,
        prefix = 16, postfix = 16, spec_frames=1, attn_all_frame=True, **kwargs)
    return model

@register_model
def compo_cast_single_beats_Bsquare_CA9_down4_base_patch16_224(pretrained=False, args=None, class_list=None, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, text_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=True, audio_enabled=True, text_num_heads=12, CA=9, output_text_dim=768,
        prefix = 16, postfix = 16, spec_frames=1, attn_all_frame=True, down_ratio=4, **kwargs)
    return model

@register_model
def compo_cast_single_beats_Bsquare_ALLCA9_down4_base_patch16_224(pretrained=False, args=None, class_list=None, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, text_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=True, audio_enabled=True, text_num_heads=12, CA=9, output_text_dim=768,
        prefix = 16, postfix = 16, spec_frames=1, attn_all_frame=True, down_ratio=4, CA_eq=True, **kwargs)
    return model

@register_model
def compo_cast_single_beats_Bsquare_CA0_base_patch16_224(pretrained=False, args=None, class_list=None, **kwargs):
    model = STCrossTransformer(
        patch_size=16, embed_dim=768, text_dim=768, depth=12, num_heads=12, mlp_ratio=4, qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6), composition=True, audio_enabled=True, text_num_heads=12, CA=0, output_text_dim=768,
        prefix = 16, postfix = 16, spec_frames=1, attn_all_frame=True, **kwargs)
    return model