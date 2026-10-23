# Nano Decoder 加速器规格

版本：1.4（设计草案）。本规格定义一个单时钟、外部存储驱动的 decoder 加速器的功能、接口和实现约束。目标交付为可综合 RTL；平台采用 Nangate45 标准单元与抽象存储宏，范围为布局布线前的功能与实现评估。物理宏和完整时序边界尚未确定，见第9节。

## 1 功能与工作模式

芯片接收RUN命令，根据本次任务的参数从外部内存读取权重和token序列，按序计算每个位置的logits与argmax，将结果写回指定区域，最后返回DONE。任务参数和外存布局须具有明确的数据交换约定，见附录C。

支持 batch=1、teacher-forced、严格因果的逐 token decode：位置 p 只能使用位置0..p的信息，输入 token 已给定，不将本次 argmax 自动反馈为下一 token。不包含 tokenizer、训练、prefill 专用加速、MTP 和 RoPE；embedding 与 LM head 不共享权重。模型使用可变的合成量化权重，不能硬编码权重、token、路由选择或输出。

计算功能由附录A定义，配套 `reference.py` 提供float32参考输出，输出形状为 `[seq_len,512]`。内部计算精度可以不同于float32，但须满足第8节的误差要求。

## 2 支持的模型配置

| 字段 | 要求 |
|---|---|
| 模型名称 | Nano Decoder |
| batch | 1 |
| n_layers / layer_pattern | 2 / `LF`，L0=KDA，L1=NoPE MLA |
| d_model / vocab | 64 / 512 |
| max_seq | 64 |
| seq_len | 8..64，公共基本工作负载为48和16 |
| KDA | 2 heads，每head32维，kernel4 depthwise causal conv |
| MLA | 2 heads，dk32、dr16、dv32、dc128 |
| MoE每层 | 8 routed experts，top_k2，1 shared expert，d_ff32 |
| attention residual block | 2 |
| group_size | 64或128；每矩阵有效group为min(group_size,fan_in) |

同一份RTL编译结果必须支持上述seq_len和group_size的运行时变化。其他模型维度和层类型固定为本表，可在编译时确定；本版本不要求支持任意模型维度、层型组合或batch，也不强制读取或校验其运行时副本。本版功能保证范围为符合本规格的合法输入，不要求非法配置检测或错误响应。

权重和小参数的张量形状、数值含义及参考文件编码见附录B。每次任务提供序列长度、量化group和输入输出位置；具体参数编码与外存布局由新设计的数据交换约定定义，不要求兼容既有镜像。

## 3 输入、输出与数值行为

输入为 seq_len 个 uint32 token ID，每个合法 ID 范围0..511。每个位置输出512个 little-endian int32 logits 和一个 uint32 argmax；argmax 是该行实际输出 logits 的最大项索引，并列取最小索引。logits 可按行采用统一正比例定标，数值精度按第8节验收。

标准权重采用 unsigned int4 q、uint8 zero point 和 int16 Q3.12 scale。小参数采用16位定点编码，详见附录B。RMSNorm eps=1e-5，L2Norm eps=1e-6，KDA decay系数=-5.0，router scale=2.828。

本规格不限定内部数值格式。有限精度、舍入及溢出处理的综合影响必须满足端到端误差要求。

## 4 复位与状态

| 算法状态 | 功能要求 |
|---|---|
| KDA S | 每head为32×32矩阵；序列开始为0，按附录A跨token更新 |
| q/k/v卷积历史 | 各保留前三个pre-conv输入；序列开始为0，三个历史独立 |
| MLA历史 | 位置p的注意力仅使用本序列位置0..p的信息；序列开始无历史 |
| residual snapshots | 按附录A在每个token重新建立，不跨token继承 |

每次RUN都必须从新序列的算法初始状态开始，不受此前任务或内部存储上电值影响。芯片须支持上电复位后的首个RUN，以及前一任务完成后的后续RUN；任务之间不强制复位。状态初始化的内部机制不由本规格规定。

## 5 顶层接口与命令协议

模块名 `msh_chip_top`，单时钟clk。rst_n为低有效同步复位，在clk上升沿生效。

| 信号 | 方向 | 位宽 |
|---|---|---:|
| clk、rst_n | input | 各1 |
| cmd_valid、cmd_data | input | 1、32 |
| cmd_ready | output | 1 |
| rsp_valid、rsp_data | output | 1、32 |
| rsp_ready | input | 1 |
| mem_req_valid、mem_req_write | output | 各1 |
| mem_req_ready | input | 1 |
| mem_req_addr、mem_req_wdata、mem_req_wstrb | output | 36、128、16 |
| mem_rsp_valid、mem_rsp_rdata | input | 1、128 |
| mem_rsp_ready | output | 1 |

valid&&ready的上升沿为一次传输。发送方在valid为1且ready为0时保持valid及payload稳定，包括cmd、rsp、内存请求及响应。RUN=32'h00000001，DONE=32'h0000D0DE。忙时不得接受另一个RUN；DONE握手完成后须能接收后续任务，不要求外部复位。任务配置若需额外命令，其编码须在数据交换约定中定义，使用本节既有物理接口。

所有有效logits和argmax的输出写请求均被接受后，才能发送DONE。若数据交换约定另含内存完成标记，其写请求也须在DONE之前被接受。本任务完成后不得再写入该任务的输出；前一任务遗留的响应不得被作为后一任务的数据。完成标记是否存在、布局及编码不由本规格指定。

周期C从RUN接受周期索引到DONE接受周期索引的差值计算，包含本次任务所需的配置读取、数据搬运、状态初始化、计算及输出开销。不能仅统计MAC忙碌周期。

## 6 外部内存接口

地址为字节地址，每次传输128位，地址16B对齐，峰值每周期接受一个请求、返回一个读 beat。little-endian：rdata[8*i+:8] 对应 addr+i。写采用 posted write，没有写响应；wstrb[i] 控制该字节是否更新。

读响应按请求接受顺序返回，不带 transaction ID，允许多个在途读请求。芯片必须在外部延迟、请求背压和响应背压变化时保持正确，有限在途队列的深度由实现确定。未接受的请求不得丢失，未被接收的响应必须保持。读数据按请求接受时的外存内容返回；禁止未对齐、越界访问和越过任务所分配区域的输出写入。

性能评估与协议验证采用以下外部存储条件。附加延迟在表中闭区间内均匀取整数；请求背压在相应工作负载中按每周期独立撤销ready建模。

| 工作负载 | 基础读延迟 | 附加延迟 | 请求背压 |
|---|---:|---:|---:|
| 48-token，吞吐统计 | 24周期 | 0..12周期 | 无 |
| 16-token，功能及协议 | 24周期 | 0..24周期 | 每周期约5% |
| 16-token，延迟变化验证 | 48周期 | 0..24周期 | 每周期约5% |

芯片按valid/ready处理传输，不能依赖固定响应周期；在请求或响应背压解除后须继续正确执行。输入输出布局遵循选定的数据交换约定，见附录C。

## 7 实现平台与资源要求

| 项目 | 要求及状态 |
|---|---|
| 标准单元平台 | Nangate45 typical，布局布线前估算 |
| 时钟目标 | 100MHz，周期10.0ns |
| 片上存储容量 | SRAM与ROM合计≤4MiB，包括实际复制实例 |
| 总估算面积预算 | ≤4.0mm²，标准单元逻辑面积与SRAM/ROM宏估算面积之和 |
| 吞吐设计目标 | ≥20000tokens/s，48-token工作负载，包含整次RUN开销 |
| 功耗预算 | 未指定 |

存储和非线性计算的实现方式不作统一限定：可采用可用存储宏、寄存器、组合逻辑或其组合。附录D规定可用的抽象存储能力，不要求每个数组或LUT都实例化宏。寄存器和组合逻辑实现的存储及LUT计入实际标准单元面积；只有实际映射到存储宏的资源才使用宏面积估算。增加端口所需的复制和外围逻辑均须计入资源，不允许用未计价的存储黑盒代替。

宏容量为 `B=sum(DEPTH_i*WIDTH_i)` bits，容量 MiB=`B/(8*2^20)`。当前抽象面积模型为 `A_macro=B/2^20` mm²，即二进制1Mbit/mm²；总面积报告 `A_logic+A_macro`。该密度是估算假设，不是物理宏库数据。总估算面积与4MiB容量上限分别检查；满足容量上限不能代替满足总面积预算。宏模型内部行为逻辑不得重复计入标准单元面积，容量与面积均按实际实例及参数统计。

功能、协议和已明确的面积、容量、时序条件为验收要求；吞吐为设计目标，单独判定是否达到。

整次 RUN 周期数为 RUN 握手到 DONE 握手的周期索引差。`CPT=C_RUN_to_DONE/seq_len`，按目标频率推算 `TPS=100e6/CPT`。20000tokens/s 等价于平均≤5000cycles/token；48-token整次RUN≤240000周期。时序闭合前，这只是目标频率下的推算，不能作为实测吞吐或达到100MHz的证明。

时序要求覆盖计算逻辑、控制逻辑、存储边界和外部接口。正式时序验收还需第9节的约束；局部模块延迟或带未约束黑盒的报告不能代表完整设计闭合。

## 8 验收条件

功能比较使用本项目的浮点参考。对每个位置，以 float64 计算：

```text
cos(row) = dot(rtl,ref)/(norm(rtl)*norm(ref))
任一 norm 为0时，cos=0
hit = ref[rtl_argmax] >= max(ref)-0.01*std(ref)
agreement = sum(hits)/sum(rows)
```

std 使用总体标准差（ddof=0）。每行 cosine≥0.98；基本48-token与16-token两次运行合并后的 agreement≥0.99。近并列容差只作用于与参考模型的比较，argmax 仍必须与芯片自身输出一致，不允许裁剪非法 ID 后评分。

功能回归须覆盖group64/128、seq48/16；边界验证覆盖seq8/64、不同权重seed、数据交换约定支持的地址变化、请求及响应背压、不复位的连续任务，以及复位后再次运行。相同输入下，随机触发器初值与零初值运行的有效logits、argmax及所定义的完成信息必须逐字节一致。

全部运行须满足输出完整、完成顺序正确、内存传输不丢失不重复、背压期间payload稳定、地址合法；外部传输持续取得进展时须完成RUN。基础读延迟增加到48周期时仍须功能正确；未规定周期变化比例上限。

RTL须可被仿真器和综合器共同解析，完成层次展开、参数检查和技术映射；不得有组合环路、未规定的锁存器或仿真专用计算旁路。存储容量、逻辑面积、宏面积及完整设计时序须按同一实现统计；总估算面积必须≤4.0mm²。当前缺少的时序条件未补齐前，不能声明完整时序验收通过。

## 9 待定项

| 待定项 | 影响 |
|---|---|
| 真实SRAM/ROM宏库及容量/宽度档位 | 物理可实现性、精确面积与宏时序 |
| 时钟uncertainty、I/O delay/load、宏timing模型 | 完整100MHz时序验收 |
| 运行中复位与外部在途事务的处理边界 | 当前尚未定义中途复位的系统行为；正常完成后的连续任务不依赖复位 |
| 功耗与物理实现约束 | 当前范围不含功耗、布局布线或签核 |

上述待定项不构成本版已完成的要求定义；当前面积为抽象平台估算，完整时序与物理实现结论须等待相应条件确定。

## 附录A 计算定义

模型无位置编码，embedding与LM head不共享权重。常数：RMSNorm eps=1e-5，L2Norm eps=1e-6，KDA decay系数=-5.0，router scale=2.828。

### A.1 层间计算与残差

```text
x = embedding[token]
blocks = []                         # 每个token重新创建
for layer in [0,1]:
    boundary = (layer % 2 == 0)
    prefix = x
    if boundary: blocks.append(x)
    mix_in = blocks[:-1] if boundary else blocks
    if mix_in: x = res_mix(x, mix_in, res_attn[layer])
    attention = mixer[layer](RMSNorm(x, norm1[layer]))
    prefix = attention if boundary else prefix + attention
    x = res_mix(prefix, blocks, res_mlp[layer])
    x = prefix + MoE[layer](RMSNorm(x, norm2[layer]))
x = res_mix(x, blocks, res_out)
logits = RMSNorm(x, final_norm) @ head
```

`res_mix(prefix,blocks)` 的候选顺序是旧snapshot在前、prefix最后。对每个候选v求 `dot(RMSNorm(v,norm_offset),proj)`，对标量集合做softmax，再混合原始v，不能混合norm之后的v。RMSNorm为 `x/sqrt(mean(x*x)+eps)*(1+offset)`。

### A.2 KDA

对norm后输入h生成q/k/v、每head beta、低秩decay输入f和output gate z：

```text
q/k/v = h @ Wq/Wk/Wv
beta = sigmoid(h @ Wb)
f = (h @ Wf1) @ Wf2                 # 中间没有激活
z = (h @ Wg1) @ Wg2
```

q/k/v分别用各自前三个pre-conv值及当前值做kernel4 depthwise conv，再经过SiLU；三个history独立。每head的q/k做L2Norm。逐通道 `alpha=exp(-5*sigmoid(A_head*(f+dt_bias)))`。每head状态为32×32：

```text
S = diag(alpha) * S
u = v - k @ S
S = S + outer(beta*k,u)
out = (q @ S)/sqrt(32)              # 必须读取更新后的S
out = RMSNorm(out,onorm_offset) * sigmoid(z)
```

onorm参数在head之间共享，gate为sigmoid。concat heads后经o投影。每个token对逻辑state/history的最终影响须与上述递推一致。

### A.3 NoPE MLA

```text
c = RMSNorm(h @ wc,knorm_offset)    # 128维，K/V共享
qc = reshape(h @ wq_c,[2,32])
qr = reshape(h @ wq_r,[2,16])
kc = reshape(c @ wk_c,[2,32])
kr = h @ wk_r                      # 16维，所有head共享
v = reshape(c @ wv,[2,32])
q_head = concat(qc_head,qr_head)
k_head = concat(kc_head,kr)
```

位置p的注意力包含当前及此前位置的k/v：对位置0..p执行 `softmax(K@q/sqrt(48)) @ V`，concat contexts后经o投影。没有RoPE、QKNorm或output gate。

### A.4 MoE

```text
s = sigmoid(h @ router)
sel = stable_top2(s + bias)         # 并列选择较小expert ID
w = s[sel]/sum(s[sel])*2.828        # 加bias的值不用于加权
expert(h) = down(SiLU(gate(h))*up(h))
out = shared(h) + sum(w_i*expert_sel_i(h))
```

shared始终参与、不乘路由权重。每token选择两个不同的routed experts。

## 附录B 张量与编码

| 标准矩阵 | fan_in×fan_out |
|---|---|
| L0.kda.q/k/v/o | 64×64 |
| L0.kda.b | 64×2 |
| L0.kda.f1/g1 | 64×32 |
| L0.kda.f2/g2 | 32×64 |
| L1.mla.wc | 64×128 |
| L1.mla.wq_c | 64×64 |
| L1.mla.wq_r | 64×32 |
| L1.mla.wk_c/wv | 128×64 |
| L1.mla.wk_r | 64×16 |
| L1.mla.o | 64×64 |
| L0/L1.moe.router | 64×8 |
| 每层moe.shared及moe.e0..e7的g/u | 64×32 |
| 每层moe.shared及moe.e0..e7的d | 32×64 |
| head | 64×512 |

标准矩阵逻辑shape为[K,N]。q为0..15的unsigned int4，s为int16 Q3.12，z为0..15的uint8；`g=min(group_size,K)`，`w[k,c]=(q[k,c]-z[k//g,c])*s[k//g,c]*2^-12`。

参考文件的`.q`数组为uint8 [K/2,N]，C-order、N最快：byte[r,c]低nibble对应q[2r,c]，高nibble对应q[2r+1,c]；`.s/.z`数组shape为[ceil(K/g),N]。这是参考文件的编码，不强制芯片外存或片上的物理排列。新数据交换约定可采用其他无损打包和排列，但必须保持上述权重数值。

embedding的逻辑shape为[512,64]。参考文件`.q`为uint8 [512,32]，沿hidden维度按低nibble为偶数索引、高nibble为奇数索引打包；`.s/.z`为[512,1]，分别int16 Q3.12和uint8。每行使用一个scale和zero point。其物理排列同样可由新数据交换约定改变。head是独立标准矩阵。

小参数的逻辑shape及16位数值编码如下；参考文件数组按C-order组织，新数据交换约定不必沿用该物理排列：

| 参数 | shape | 解码 |
|---|---|---|
| 每层res_attn.norm.w/res_mlp.norm.w、norm1.w/norm2.w、res_out.norm.w、final_norm.w | [64] | signed Q2.13 offset，gain=1+v*2^-13 |
| L1.mla.knorm.w | [128] | 同上 |
| L0.kda.onorm.w | [32] | 同上，head共享 |
| 每层res_attn.proj/res_mlp.proj、res_out.proj | [64] | signed Q4.11 |
| L0.kda.qconv/kconv/vconv | [64,4] | signed Q4.11，tap从最旧history到当前 |
| L0.kda.dtb | [64] | signed Q4.11 |
| 每层moe.bias | [8] | signed Q4.11 |
| L0.kda.A | [2] | unsigned Q8.8 |

## 附录C 运行参数与数据交换要求

本项目不要求兼容既有的镜像格式。控制及数据交换约定必须明确每次任务的seq_len、group_size、权重和小参数、token输入、logits与argmax输出位置；若使用外存临时空间，还须定义其位置与可用范围。模型固定维度不要求重复编码为运行参数。

具体配置编码、参数传递方式、张量排列、对齐padding及输出寻址由新设计定义，使用第5、6节的既有物理接口。36位外存地址不受旧32位镜像地址字段限制。不预设header的大小或位置、descriptor、名称hash、张量排列顺序、行stride或内存完成标记；数据交换约定一经确定，合法输入、RTL与数据打包/解包必须使用同一版本。

数据交换必须完整保留附录B的数值含义，不得改变权重分布、量化值或token内容以适应实现。输入参数和有效输出须可准确定位；输入、输出和临时空间的分配范围须明确，访问不得越界。输出区域和临时空间的初始内容不得影响有效结果。外存beat地址仍按第6节16B对齐。

## 附录D 可用抽象存储能力

以下是本次评估平台可用的抽象存储原语，不是要求采用的内部存储架构。存储数量、bank划分、容量、位宽及选用方式由实现决定。该平台用于功能仿真和面积估算，尚不代表真实foundry宏库。

| 原语 | 可用能力 |
|---|---|
| SRAM | 单时钟1R1W，同一上升沿可读写；同步读，读延迟1周期；未读时输出保持；按字节写使能；读写同址返回旧数据 |
| ROM | 两个异步读口，无写口；内容初始化后固定且全部有效地址有确定数据 |

原语容量为DEPTH×WIDTH bits，DEPTH≥2，WIDTH为8的正倍数；访问地址须在深度范围内。SRAM没有数据复位或默认初值。ROM的初始化数据必须符合所实现的计算。RTL包装名称、端口名称和初始化文件加载方式不属于芯片功能约束，但模型和实现必须满足表中的能力与语义。

可以不使用上述ROM，也不要求所有常量或LUT通过ROM实现。采用寄存器或组合逻辑时按实际标准单元面积计入；采用宏时按实际实例及参数计入容量和宏面积，不能重复计算宏内部行为模型的逻辑。推断存储只有明确映射到这些原语并保留容量、端口及时序语义时，才能采用对应宏的估算口径。

该平台尚未规定真实宏的容量/位宽档位、访问功耗或精确timing arc。任何不同于表中能力的存储原语都需明确相应成本和时序条件，不能默认为可用或无成本。
