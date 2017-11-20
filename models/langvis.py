# -*- coding: utf-8 -*-

"""
Query-based Scene Segmentation (QSegNet) Network PyTorch implementation.
"""

import torch
from sru import SRU
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from .dpn.model_factory import create_model


class LangVisNet(nn.Module):
    def __init__(self, dict_size, emb_size=1000, hid_size=1000,
                 vis_size=2688, num_filters=1, num_mixed_channels=1,
                 mixed_size=1000, hid_mixed_size=1005, backend='dpn92',
                 pretrained=True, extra=True, msru_layers=3):
        super().__init__()
        self.vis_size = vis_size
        self.num_filters = num_filters
        self.base = create_model(
            backend, 1, pretrained=pretrained, extra=extra)

        self.emb = nn.Embedding(dict_size, emb_size)
        self.sru = SRU(emb_size, hid_size)

        self.adaptative_filter = nn.Linear(
            in_features=hid_size, out_features=(num_filters * (vis_size + 2)))

        self.comb_conv = nn.Conv2d(in_channels=(2 + emb_size + hid_size +
                                                vis_size + num_filters),
                                   out_channels=mixed_size,
                                   kernel_size=1,
                                   padding=0)

        self.msru = SRU(mixed_size, hid_mixed_size,
                        num_layers=msru_layers)
        self.output_collapse = nn.Conv2d(in_channels=hid_mixed_size,
                                         out_channels=1,
                                         kernel_size=1)

    def forward(self, vis, lang):
        B, C, H, W = vis.size()
        # print('vis size: ',vis.size())
        vis = self.base(vis)
        # print('vis output size: ',vis.size())

        # LxE ?
        lang_mix = []
        lang = self.emb(lang)
        lang = torch.transpose(lang, 0, 1)
        # print('lang (embeddings) size: ',lang.size())
        temp_expanded = lang.unsqueeze(-1).unsqueeze(-1).expand(lang.size(0), lang.size(1), lang.size(2),
                                     vis.size(-2), vis.size(-1))
        # print('1. temp_expanded size: ',temp_expanded.size())
        lang_mix.append(temp_expanded)
        # print('lang mix size: ',len(lang_mix))
        # lang will be of size LxH
        # input has dimensions: seq_length x batch_size x we_dim
        lang, _ = self.sru(lang)
        # print('lang (output of SRU) size: ',lang.size())
        time_steps = lang.size(0)
        # print('time steps: ',time_steps)
        temp_expanded = lang.unsqueeze(-1).unsqueeze(-1).expand(lang.size(0), lang.size(1), lang.size(2), 
                                     vis.size(-2), vis.size(-1))
        # print('2. temp_expanded size: ',temp_expanded.size())
        lang_mix.append(temp_expanded)
        # print('lang mix size: ',len(lang_mix))

        # Lx(H + E)xH/32xW/32
        lang_mix = torch.cat(lang_mix, dim=2)
        # print('lang mix size (after concat): ',lang_mix.size())
        
        out_h, out_w = vis.size(2), vis.size(3)
        x = Variable(torch.linspace(start=-1, end=1, steps=out_w).cuda())
        x = x.unsqueeze(0).expand(out_h, out_w).unsqueeze(0).unsqueeze(0)

        y = Variable(torch.linspace(start=-1, end=1, steps=out_h).cuda())
        y = y.unsqueeze(1).expand(out_h, out_w).unsqueeze(0).unsqueeze(0)

        # print('x coords size: ',x.size())
        # print('y coords size: ',y.size())

        # (N + 2)xH/32xW/32
        vis = torch.cat([vis, x, y], dim=1)
        # print('vis size: ',vis.size())

        # Size: HxL?
        lang = lang.squeeze()
        # print('lang size (after squeeze): ',lang.size())
        # filters dim: (F * (N + 2))xL
        filters = self.adaptative_filter(lang)
        filters = F.sigmoid(filters)
        # print('filters size: ',filters.size())
        # LxFx(N+2)x1x1
        filters = filters.view(
            time_steps, self.num_filters, self.vis_size + 2, 1, 1)
        # print('filters size (after view): ',filters.size())
        p = []
        for t in range(time_steps):
            filter = filters[t]
            p.append(F.conv2d(input=vis, weight=filter).unsqueeze(0))

        # print('p size: ',len(p))

        # LxFxH/32xW/32
        p = torch.cat(p)
        # print('p size (after concat): ',p.size())

        # Lx(N + 2)xH/32xW/32
        vis = vis.unsqueeze(0).expand(time_steps, *vis.size())
        # print('vis size after unsqueeze and expand: ',vis.size())
        # Lx(N + F + H + E + 2)xH/32xW/32
        mixed = torch.cat([vis, lang_mix, p], dim=2)
        # print('mixed size: ',mixed.size())
        # LxSxH/32xW/32
        q = []
        for t in range(time_steps):
            q.append(self.comb_conv(mixed[t]).unsqueeze(0))
        # print('len(q): ',len(q))
        # # print('q[1].size(): ',q[1].size())
        q = torch.cat(q)
        # print('q size: ',q.size())
        # LxSx((H + W)/32)
        q = q.view(q.size(0), q.size(1), q.size(2),
                           q.size(3) * q.size(4))
        # print('1', q.size())
        q = torch.transpose(q, 3, 0)
        q = torch.transpose(q, 3, 1)
        q = torch.transpose(q, 3, 2).contiguous()
        q = q.view(q.size(0) * q.size(1), q.size(2), q.size(3))

        # print('q size after view: ',q.size())
        # input has dimensions: seq_length x batch_size x we_dim
        # IDK
        output, _ = self.msru(q)
        # print('1 output size: ',output.size())
        # Take all the hidden states (one for each pixel of every 
        # 'length of the sequence') but keep only the last out_h * out_w
        # so that it can be reshaped to an image of such size
        output = output[-(out_h * out_w):,:,:]
        # print('2 output size: ',output.size())
        output = torch.transpose(output, 0, 1)
        output = torch.transpose(output, 1, 2).contiguous()
        # print('3 output size: ',output.size())
        output = output.view(output.size(0), output.size(1),
                             out_h, out_w)
        # print('3 output size: ',output.size())

        output = self.output_collapse(output)
        # print('final output size: ',output.size())
        return output
