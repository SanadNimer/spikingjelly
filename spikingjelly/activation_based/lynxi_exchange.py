import copy
import os
from typing import Dict
import torch
import torch.nn as nn
import torch.nn.functional as F
import logging
import numpy as np
from . import neuron, functional, layer

'''
TracerWarning: Converting a tensor to a Python index might cause the trace to be incorrect. We can't record the data flow of Python values, so this value will be treated as a constant in the future. This means that the trace might not generalize to other inputs!
  for t in range(x_seq.shape[0]):

不支持inplace操作，因此形如x[t] = y之类的操作都无效，x[t]并不会被设置为y，且不会报错

不支持5D的tensor参与模型编译，在任何位置都不能出现超过4D的tensor

'''

# def unfold_seq(T: int, x_seq_folded: torch.Tensor):
#     # x_seq.shape = [TN, *]
#     # 此函数会因未知原因编译报错
#     return x_seq_folded.reshape(T, x_seq_folded.shape[0] // T, *x_seq_folded.shape[1:])
#

class BaseNode(nn.Module):
    def __init__(self, v_threshold: float = 1., v_reset: float = 0., step_mode='s', T: int = None,
                 return_v: bool = False):
        super().__init__()
        self.v_threshold = v_threshold
        self.v_reset = v_reset
        self.step_mode = step_mode
        self.T = T
        self.return_v = return_v

    def neuronal_charge(self, x: torch.Tensor, v: torch.Tensor):
        raise NotImplementedError

    def single_step_forward(self, x: torch.Tensor, v: torch.Tensor = None):
        if v is None:
            v = torch.zeros_like(x)
        v = self.neuronal_charge(x, v)

        spike = (v >= self.v_threshold).to(x)
        if self.v_reset is None:
            v = v - spike * self.v_threshold
        else:
            v = (1. - spike) * v + spike * self.v_reset

        return spike, v

    def multi_step_forward(self, x_seq: torch.Tensor, v_init: torch.Tensor = None):
        if v_init is None:
            v = torch.zeros_like(x_seq[0])
        else:
            v = v_init
        spike_seq = []
        for t in range(self.T):
            spike, v = self.single_step_forward(x_seq[t], v)
            spike_seq.append(spike.unsqueeze(0))

        spike_seq = torch.cat(spike_seq)
        return spike_seq, v

    def forward(self, x: torch.Tensor, v: torch.Tensor = None):
        if self.step_mode == 's':
            spike, v = self.single_step_forward(x, v)
            if self.return_v:
                return spike, v
            else:
                return spike
        elif self.step_mode == 'm':
            x_shape = x.shape

            # 起始 编译通过-------------------
            x = x.reshape(self.T, x.shape[0] // self.T, -1)
            # 终结 编译通过-------------------

            # 起始 编译报错-------------------
            # x = x.flatten(1)
            # x = unfold_seq(self.T, x)
            # 终结 编译报错-------------------


            if v is not None:
                v = v.flatten()
            spike_seq, v = self.multi_step_forward(x, v)

            spike_seq = spike_seq.flatten(0, 1).reshape(x_shape)

            if self.return_v:
                v = v.reshape([x_shape[0] // self.T] + list(x_shape[1:]))
                return spike_seq, v
            else:
                return spike_seq


class IFNode(BaseNode):
    def neuronal_charge(self, x: torch.Tensor, v: torch.Tensor):
        return x + v

class LIFNode(BaseNode):
    def __init__(self, tau: float = 2., decay_input: bool = True, v_threshold: float = 1.,
                 v_reset: float = 0., step_mode='s'):
        super().__init__(v_threshold, v_reset, step_mode)
        self.decay = 1. / self.tau
        self.decay_input = decay_input

    def neuronal_charge(self, x: torch.Tensor, v: torch.Tensor):

        if self.v_reset is None:
            v = (1. - self.decay) * v
        else:
            v = (1. - self.decay) * (v - self.v_reset)

        if self.decay_input:
            x = x * self.decay

        return v + x




def to_lynxi_supported_modules(net: list or tuple or nn.Sequential, T: int):
    output_net = []
    for i in range(net.__len__()):
        m_in = net[i]
        m_out = None

        if isinstance(m_in, layer.Conv2d):
            m_out = nn.Conv2d(in_channels=m_in.in_channels, out_channels=m_in.out_channels,
                              kernel_size=m_in.kernel_size, stride=m_in.stride, padding=m_in.padding,
                              dilation=m_in.dilation, groups=m_in.groups, bias=m_in.bias is not None,
                              padding_mode=m_in.padding_mode)

            m_out.weight.data = m_in.weight.data.cpu().clone()
            if m_in.bias is not None:
                m_out.bias.data = m_in.bias.data.cpu().clone()


        elif isinstance(m_in, layer.BatchNorm2d):
            m_out = nn.BatchNorm2d(num_features=m_in.num_features, eps=m_in.eps, momentum=m_in.momentum,
                                   affine=m_in.affine, track_running_stats=m_in.affine)
            if m_in.weight is not None:
                m_out.weight.data = m_in.weight.data.cpu().clone()
            if m_in.bias is not None:
                m_out.bias.data = m_in.bias.data.cpu().clone()


        elif isinstance(m_in, layer.MaxPool2d):
            m_out = nn.MaxPool2d(kernel_size=m_in.kernel_size, stride=m_in.stride, padding=m_in.padding,
                                 dilation=m_in.dilation, return_indices=m_in.return_indices, ceil_mode=m_in.ceil_mode)


        elif isinstance(m_in, layer.AvgPool2d):
            m_out = nn.AvgPool2d(kernel_size=m_in.kernel_size, stride=m_in.stride, padding=m_in.padding,
                                 ceil_mode=m_in.ceil_mode, count_include_pad=m_in.count_include_pad,
                                 divisor_override=m_in.divisor_override)


        elif isinstance(m_in, layer.AdaptiveAvgPool2d):
            m_out = nn.AdaptiveAvgPool2d(output_size=m_in.output_size)


        elif isinstance(m_in, layer.Flatten):
            m_out = nn.Flatten(start_dim=m_in.start_dim, end_dim=m_in.end_dim)


        elif isinstance(m_in, neuron.IFNode):
            m_out = IFNode(v_threshold=m_in.v_threshold, v_reset=m_in.v_reset, step_mode=m_in.step_mode, T=T,
                           return_v=False)

        else:
            m_out = copy.deepcopy(m_in).cpu()

        output_net.append(m_out)

    return output_net


try:
    '''
    适配灵汐科技的芯片

    '''
    import lyngor
    import lynpy


    def torch_tensor_to_lynxi(x: torch.Tensor, device_id: int = 0, to_apu: bool = True):
        x_size_in_byte = x.element_size() * x.numel()
        x = x.cpu().detach().numpy()
        x = lynpy.Tensor(dev_id=device_id, size=x_size_in_byte).from_numpy(x)
        if to_apu:
            x = x.apu()
        return x


    def lynxi_tensor_to_torch(x: lynpy.Tensor, shape: tuple or list = None, dtype: str = None):
        if shape is not None and dtype is not None:
            x = x.view_as(shape, dtype)
        if x.devptr is not None:
            x = x.cpu()
        x = torch.from_numpy(x.numpy())
        return x


    def compile_lynxi_model(output_dir: str, net: nn.Module, in_data_type: str = 'float32', out_data_type: str = 'float32', input_shape_dict : Dict = None):
        model = lyngor.DLModel()
        model.load(net, model_type='Pytorch', in_type=in_data_type, out_type=out_data_type,
                    inputs_dict=input_shape_dict)
        offline_builder = lyngor.Builder(target='apu')
        out_path = offline_builder.build(model.graph, model.params,
                                out_path=output_dir)
        print(os.listdir(out_path))
        return os.path.join(out_path, 'Net_0')

    def load_lynxi_model(device_id: int, model_path: str):
        return lynpy.Model(dev_id=device_id, path=model_path)


except BaseException as e:
    logging.info(f'spikingjelly.activation_based.lynxi_exchange: {e}')

'''
代码示例：

from spikingjelly.activation_based import lynxi_exchange, layer, functional, neuron
import torch
import torch.nn as nn
import os
import numpy as np


T = 8
N = 2
temp_dir = '/home/cxhpc/fangwei/tempdir'
module = nn.Sequential(
    layer.Conv2d(3, 4, kernel_size=3, stride=1, padding=1, bias=False, step_mode='m'),
    layer.BatchNorm2d(4, step_mode='m'),
    neuron.IFNode(step_mode='m'),
)

x = torch.rand([T, N, 3, 16, 16])
x = x.flatten(0, 1)
print(x.shape)
module.eval()
with torch.no_grad():
    y_torch = module(x.reshape([T, N] + list(x.shape[1:])))
    print(y_torch)
    print(y_torch.shape)

module = nn.Sequential(*lynxi_exchange.to_lynxi_supported_modules(module, T))
print(x.shape, module(x).shape)
print(module)

out_path = lynxi_exchange.compile_lynxi_model(os.path.join(temp_dir, '1'), module, in_data_type='float32', out_data_type='float32', input_shape_dict={'x': x.shape})



print(out_path)
device_id = 1
lynxi_model = lynxi_exchange.load_lynxi_model(device_id, out_path)


with torch.no_grad():
    x = lynxi_exchange.torch_tensor_to_lynxi(x, device_id=device_id)
    y_lynxi = lynxi_model(x)
    y_v_lynxi_to_torch = lynxi_exchange.lynxi_tensor_to_torch(y_lynxi, y_torch.shape, np.float32)
    print(y_v_lynxi_to_torch)
    print((y_v_lynxi_to_torch - y_torch).abs().mean())

'''

