// Synthesis-only blackbox for the q4_dequant_matvec_ch leaf.
//
// The golden closure instantiates hundreds of CHAN_BASE-parameterized leaves;
// deriving every unique paramod in the preflight hierarchy pass is what used
// to exhaust the 600s gate.  The preflight flow reads this file with
// `read_verilog -lib`, so hierarchy resolves each leaf instantiation to an
// empty cell while the measure flow (which never reads this file) still
// elaborates the real body from rtl/q4_dequant_matvec.sv.
//
// Parameters and ports must stay byte-for-byte in sync with the real module.
(* blackbox *)
module q4_dequant_matvec_ch #(
  parameter int IN        = 64,
  parameter int OUT       = 64,
  parameter int GROUP     = 64,
  parameter int DATA_W    = 24,
  parameter int FRAC_W    = 14,
  parameter int CHAN_BASE = 0,
  parameter int NCHAN     = 1,
  localparam int GROUPS   = (IN + GROUP - 1) / GROUP
) (
  input  logic signed [IN*DATA_W-1:0]      x_bus,
  input  logic        [(IN/2)*OUT*8-1:0]   q_bus,
  input  logic signed [GROUPS*OUT*16-1:0]  scale_bus, // Q3.12
  input  logic        [GROUPS*OUT*8-1:0]   zero_bus,
  output logic signed [NCHAN*DATA_W-1:0]   y
);
endmodule

// The remaining golden-closure data plane is blackboxed for the same reason:
// the preflight `hierarchy` pass must only derive control/glue (top, header,
// micro-op controller, datapath FSM, adapter, logit writer, arbiter, MLA cache
// attention contract, mla_kv_cache) plus the $mem kept for SRAM macro
// accounting.  Every module below is pure combinational/sequential data path
// whose aggregate genrtlil volume (roughly 130k MAC [INFERENCE]) exceeds the
// 600 s preflight gate.  The measure flow never reads this file, so it still
// elaborates each real body from rtl/.
//
// Parameters and ports below stay byte-for-byte in sync with the real modules.

(* blackbox *)
module q4_dequant_matvec #(
  parameter int IN       = 64,
  parameter int OUT      = 64,
  parameter int GROUP    = 64,
  parameter int DATA_W   = 24,
  parameter int FRAC_W   = 14,
  localparam int GROUPS  = (IN + GROUP - 1) / GROUP,
  localparam int CHAN_BLK = 8
) (
  input  logic signed [IN*DATA_W-1:0]          x_bus,
  input  logic        [(IN/2)*OUT*8-1:0]       q_bus,
  input  logic signed [GROUPS*OUT*16-1:0]      scale_bus, // Q3.12
  input  logic        [GROUPS*OUT*8-1:0]       zero_bus,
  output logic signed [OUT*DATA_W-1:0]         y_bus
);
endmodule

(* blackbox *)
module q4_matvec_128x64 #(
  parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic group_is_128,
  input logic signed [128*DATA_W-1:0] x_bus,
  input logic [128/2*64*8-1:0] q_bus,
  input logic signed [2*64*16-1:0] s_bus,
  input logic [2*64*8-1:0] z_bus,
  output logic signed [64*DATA_W-1:0] y_bus
);
endmodule

(* blackbox *)
module q4_embedding_lookup #(
  parameter int VOCAB  = 512,
  parameter int DIM    = 64,
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic [$clog2(VOCAB)-1:0]   token_id,
  input  logic [VOCAB*(DIM/2)*8-1:0] q_bus,
  input  logic signed [VOCAB*16-1:0] scale_bus,
  input  logic [VOCAB*8-1:0]         zero_bus,
  output logic signed [DIM*DATA_W-1:0] embedding_bus
);
endmodule

(* blackbox *)
module rmsnorm_q #(
  parameter int DIM          = 64,
  parameter int DATA_W       = 24,
  parameter int FRAC_W       = 14,
  parameter int NORM_W       = 16,
  parameter int NORM_FRAC_W  = 13,
  parameter logic [63:0] EPS_Q2FRAC = 64'd2684
) (
  input  logic signed [DIM*DATA_W-1:0] x_bus,
  input  logic signed [DIM*NORM_W-1:0] norm_offset_bus,
  output logic signed [DIM*DATA_W-1:0] y_bus
);
endmodule

(* blackbox *)
module l2norm_q #(
  parameter int DIM          = 32,
  parameter int DATA_W       = 24,
  parameter int FRAC_W       = 14,
  parameter logic [63:0] EPS_Q2FRAC = 64'd268
) (
  input  logic signed [DIM*DATA_W-1:0] x_bus,
  output logic signed [DIM*DATA_W-1:0] y_bus
);
endmodule

(* blackbox *)
module softmax_vector_pwl #(
  parameter int MAX_ELEMS = 64,
  parameter int DATA_W    = 24,
  parameter int FRAC_W    = 14,
  localparam int COUNT_W  = $clog2(MAX_ELEMS + 1)
) (
  input  logic [COUNT_W-1:0]                 count,
  input  logic signed [MAX_ELEMS*DATA_W-1:0] score_bus,
  output logic        [MAX_ELEMS*(FRAC_W+1)-1:0] probability_bus
);
endmodule

(* blackbox *)
module sigmoid_pwl #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic        [FRAC_W:0]   y_out
);
endmodule

(* blackbox *)
module silu_pwl #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic signed [DATA_W-1:0] y_out
);
endmodule

(* blackbox *)
module causal_conv4 #(
  parameter int CHANNELS = 64,
  parameter int DATA_W   = 24,
  parameter int FRAC_W   = 14
) (
  input  logic                              clk,
  input  logic                              rst_n,
  input  logic                              in_valid,
  input  logic signed [CHANNELS*DATA_W-1:0] in_bus,
  input  logic signed [CHANNELS*4*16-1:0]   taps_bus, // each tap is Q4.11
  output logic signed [CHANNELS*DATA_W-1:0] pre_silu_bus
);
endmodule

(* blackbox *)
module kda_delta_head #(
  parameter int DH      = 32,
  parameter int DATA_W  = 24,
  parameter int FRAC_W  = 14
) (
  input  logic                         clk,
  input  logic                         rst_n,
  input  logic                         step_valid,
  input  logic signed [DH*DATA_W-1:0]  q_in,
  input  logic signed [DH*DATA_W-1:0]  k_in,
  input  logic signed [DH*DATA_W-1:0]  v_in,
  input  logic signed [DH*DATA_W-1:0]  alpha_in,
  input  logic signed [DATA_W-1:0]     beta_in,
  output logic signed [DH*DATA_W-1:0]  out_bus
);
endmodule

// kda_delta_col is one column of the 32-dim KDA head (rtl/kda_delta_col.sv).
// Round-29 (measurement completion): the single column is the round's hard
// wall -- attempt4 recorded LEAF_PRE_ABC_STAT = 1,537,408 cells (AND 747,881
// / XOR 499,494 / OR 260,213 / MUX 13,475 / NOT 15,577 / DFFR 768, wire bits
// 2,144,531) with ABC still un-converged at pass 58.  It is abstracted in the
// KDA-closure partitions (kda_recurrence_contract, kda_delta_head) so those
// closures map their wrapper glue, while the standalone kda_delta_col
// partition below records the real column's TOP_PRE_ABC_STAT before its own
// bounded ABC window.  Parameters and ports must stay byte-for-byte in sync
// with the real module.
(* blackbox *)
module kda_delta_col #(
  parameter int DH      = 32,
  parameter int DATA_W  = 24,
  parameter int FRAC_W  = 14
) (
  input  logic                          clk,
  input  logic                          rst_n,
  input  logic                          step_valid,
  input  logic signed [DH*DATA_W-1:0]   q_in,
  input  logic signed [DH*DATA_W-1:0]   k_in,
  input  logic signed [DATA_W-1:0]      v_in,
  input  logic signed [DH*DATA_W-1:0]   alpha_in,
  input  logic signed [DATA_W-1:0]      beta_in,
  output logic signed [DATA_W-1:0]      out_bus
);
endmodule

(* blackbox *)
module kda_recurrence_contract #(
  parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic clk, input logic rst_n, input logic step_valid,
  input logic signed [64*DATA_W-1:0] q_pre_bus, k_pre_bus, v_pre_bus,
  input logic signed [64*4*16-1:0] qconv_bus, kconv_bus, vconv_bus,
  input logic signed [2*DATA_W-1:0] b_bus,
  input logic signed [64*DATA_W-1:0] f_bus, z_bus,
  input logic [2*16-1:0] A_bus,                    // Q8.8 unsigned
  input logic signed [64*16-1:0] dtb_bus,          // Q4.11
  input logic signed [32*16-1:0] onorm_offset_bus, // Q2.13, shared per head
  output logic signed [64*DATA_W-1:0] core_bus
);
endmodule

(* blackbox *)
module swiglu_expert #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [64*DATA_W-1:0] h_bus,
  input  logic [64/2*32*8-1:0]        g_q_bus,
  input  logic signed [32*16-1:0]     g_s_bus,
  input  logic [32*8-1:0]             g_z_bus,
  input  logic [64/2*32*8-1:0]        u_q_bus,
  input  logic signed [32*16-1:0]     u_s_bus,
  input  logic [32*8-1:0]             u_z_bus,
  input  logic [32/2*64*8-1:0]        d_q_bus,
  input  logic signed [64*16-1:0]     d_s_bus,
  input  logic [64*8-1:0]             d_z_bus,
  output logic signed [64*DATA_W-1:0] out_bus
);
endmodule

(* blackbox *)
module moe_shared_top2_contract #(
  parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic signed [8*DATA_W-1:0] router_pre_bus,
  input logic signed [8*DATA_W-1:0] bias_bus,
  input logic signed [64*DATA_W-1:0] shared_bus, expert0_bus, expert1_bus,
  output logic [2:0] expert0, expert1,
  output logic signed [DATA_W-1:0] weight0, weight1,
  output logic signed [64*DATA_W-1:0] out_bus
);
endmodule

(* blackbox *)
module moe_top2_router #(
  parameter int EXPERTS = 8,
  parameter int SCORE_W = 24,
  parameter int FRAC_W  = 14
) (
  input  logic signed [EXPERTS*SCORE_W-1:0] score_bus,
  input  logic signed [EXPERTS*SCORE_W-1:0] bias_bus,
  output logic [$clog2(EXPERTS)-1:0]        expert0,
  output logic [$clog2(EXPERTS)-1:0]        expert1,
  output logic signed [SCORE_W-1:0]         weight0,
  output logic signed [SCORE_W-1:0]         weight1
);
endmodule

(* blackbox *)
module residual_mix2 #(
  parameter int DIM     = 64,
  parameter int DATA_W  = 24,
  parameter int FRAC_W  = 14
) (
  input  logic signed [DIM*DATA_W-1:0] in0_bus,
  input  logic signed [DIM*DATA_W-1:0] in1_bus,
  input  logic signed [FRAC_W:0]       weight0,
  input  logic signed [FRAC_W:0]       weight1,
  output logic signed [DIM*DATA_W-1:0] out_bus
);
endmodule

(* blackbox *)
module residual_mix2_contract #(
  parameter int DIM = 64, parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic signed [DIM*DATA_W-1:0] block_bus,
  input logic signed [DIM*DATA_W-1:0] prefix_bus,
  input logic signed [DIM*16-1:0] norm_offset_bus,  // Q2.13 (weight - 1)
  input logic signed [DIM*16-1:0] proj_bus,         // Q4.11
  output logic signed [DATA_W-1:0] block_score,
  output logic signed [DATA_W-1:0] prefix_score,
  output logic signed [DIM*DATA_W-1:0] out_bus
);
endmodule

(* blackbox *)
module residual_mix2_scores #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] score0,
  input  logic signed [DATA_W-1:0] score1,
  output logic        [FRAC_W:0]   weight0,
  output logic        [FRAC_W:0]   weight1
);
endmodule

(* blackbox *)
module argmax_vector #(
  parameter int ELEMS  = 512,
  parameter int DATA_W = 32,
  localparam int INDEX_W = $clog2(ELEMS)
) (
  input  logic signed [ELEMS*DATA_W-1:0] values_bus,
  output logic [INDEX_W-1:0]             index_out,
  output logic signed [DATA_W-1:0]       value_out
);
endmodule

(* blackbox *)
module mla_attention_head #(
  parameter int MAX_SEQ = 64,
  parameter int KD      = 48,
  parameter int DV      = 32,
  parameter int DATA_W  = 24,
  parameter int FRAC_W  = 14,
  localparam int POS_W  = $clog2(MAX_SEQ)
) (
  input  logic                        clk,
  input  logic                        rst_n,
  input  logic                        start,
  input  logic [POS_W:0]              valid_count,
  input  logic signed [KD*DATA_W-1:0] query_bus,
  output logic [POS_W-1:0]            cache_index,
  input  logic signed [KD*DATA_W-1:0] cache_key_bus,
  input  logic signed [DV*DATA_W-1:0] cache_value_bus,
  output logic                        busy,
  output logic                        done,
  output logic signed [DV*DATA_W-1:0] context_bus
);
endmodule

(* blackbox *)
module mla_latent_stage #(
  parameter int DATA_W = 24, parameter int FRAC_W = 14
) (
  input logic signed [128*DATA_W-1:0] wc_pre_bus,
  input logic signed [128*16-1:0] knorm_offset_bus,
  input logic signed [64*DATA_W-1:0] qc_pre_bus,
  input logic signed [32*DATA_W-1:0] qr_pre_bus,
  input logic signed [16*DATA_W-1:0] kr_pre_bus,
  output logic signed [128*DATA_W-1:0] latent_bus,
  output logic signed [2*48*DATA_W-1:0] query_bus,
  output logic signed [2*16*DATA_W-1:0] shared_kr_bus
);
endmodule

(* blackbox *)
module qsz_tensor_loader #(
  parameter int MAX_Q_BEATS = 1024,
  parameter int MAX_S_BEATS = 64,
  parameter int MAX_Z_BEATS = 64
) (
  input logic clk, input logic rst_n,
  input logic [35:0] descriptor_base, input logic [31:0] descriptor_count,
  input logic start, output logic busy, output logic loaded, output logic error,
  input logic [31:0] q_name_hash, input logic [31:0] q_bytes,
  input logic [31:0] s_name_hash, input logic [31:0] s_bytes,
  input logic [31:0] z_name_hash, input logic [31:0] z_bytes,
  output logic [MAX_Q_BEATS*128-1:0] q_bus,
  output logic [MAX_S_BEATS*128-1:0] s_bus,
  output logic [MAX_Z_BEATS*128-1:0] z_bus,
  output logic desc_mem_req_valid, input logic desc_mem_req_ready,
  output logic [35:0] desc_mem_req_addr, input logic desc_mem_rsp_valid,
  output logic desc_mem_rsp_ready, input logic [127:0] desc_mem_rsp_rdata,
  output logic dma_mem_req_valid, input logic dma_mem_req_ready,
  output logic [35:0] dma_mem_req_addr, input logic dma_mem_rsp_valid,
  output logic dma_mem_rsp_ready, input logic [127:0] dma_mem_rsp_rdata
);
endmodule

(* blackbox *)
module p16_tensor_loader #(
  parameter int MAX_BEATS = 64
) (
  input logic clk, input logic rst_n,
  input logic [35:0] descriptor_base, input logic [31:0] descriptor_count,
  input logic start, input logic [31:0] name_hash, input logic [31:0] byte_count,
  output logic busy, output logic loaded, output logic error,
  output logic [MAX_BEATS*128-1:0] p_bus,
  output logic desc_mem_req_valid, input logic desc_mem_req_ready,
  output logic [35:0] desc_mem_req_addr, input logic desc_mem_rsp_valid,
  output logic desc_mem_rsp_ready, input logic [127:0] desc_mem_rsp_rdata,
  output logic dma_mem_req_valid, input logic dma_mem_req_ready,
  output logic [35:0] dma_mem_req_addr, input logic dma_mem_rsp_valid,
  output logic dma_mem_rsp_ready, input logic [127:0] dma_mem_rsp_rdata
);
endmodule

(* blackbox *)
module descriptor_dma_engine (
  input logic clk, input logic rst_n,
  input logic [35:0] descriptor_base, input logic [31:0] descriptor_count,
  input logic req_valid, output logic req_accept,
  input logic [31:0] req_name_hash, input logic [31:0] req_byte_count,
  input logic req_check_size,
  output logic data_valid, input logic data_ready, output logic [127:0] data,
  output logic data_last, output logic done, output logic error,
  output logic desc_mem_req_valid, input logic desc_mem_req_ready,
  output logic [35:0] desc_mem_req_addr, input logic desc_mem_rsp_valid,
  output logic desc_mem_rsp_ready, input logic [127:0] desc_mem_rsp_rdata,
  output logic dma_mem_req_valid, input logic dma_mem_req_ready,
  output logic [35:0] dma_mem_req_addr, input logic dma_mem_rsp_valid,
  output logic dma_mem_rsp_ready, input logic [127:0] dma_mem_rsp_rdata
);
endmodule

// descriptor_lookup: the 512-set 2-way name_hash->index cache inside
// descriptor_dma_engine (rtl/descriptor_lookup.sv).  Round-38 (measurement
// completion, end-to-end convergence): its cache_valid/cache_hash/cache_index
// are unpacked arrays read combinationally asynchronously (`cache_*[w]
// [wanted_name_hash[8:0]]`), which Yosys/ABC unrolls into a ~266k-gate mux
// tree and reports as a millisecond wire-load artifact (3.26e9 ps, no final
// area row) whenever the tensor-loader closures (p16_tensor_loader,
// qsz_tensor_loader) map the module's real body.  It is now blackboxed inside
// those two loader closures so they map their own glue in a bounded window,
// and the real body is measured by the standalone `descriptor_lookup` leaf
// partition.  Parameters and ports must stay byte-for-byte in sync with the
// real module.
(* blackbox *)
module descriptor_lookup (
  input  logic         clk,
  input  logic         rst_n,
  input  logic         start,
  input  logic [35:0]  descriptor_base,
  input  logic [31:0]  descriptor_count,
  input  logic [31:0]  wanted_name_hash,
  output logic         busy,
  output logic         found,
  output logic         failed,
  output logic [35:0]  tensor_addr,
  output logic [31:0]  tensor_bytes,
  output logic [31:0]  tensor_aux,
  output logic         mem_req_valid,
  input  logic         mem_req_ready,
  output logic [35:0]  mem_req_addr,
  input  logic         mem_rsp_valid,
  output logic         mem_rsp_ready,
  input  logic [127:0] mem_rsp_rdata
);
endmodule

(* blackbox *)
module matrix_qsz_buffer #(
  parameter int MAX_Q_BEATS = 1024,
  parameter int MAX_S_BEATS = 64,
  parameter int MAX_Z_BEATS = 64
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic             begin_load,
  input  logic [15:0]      expected_q_beats,
  input  logic [15:0]      expected_s_beats,
  input  logic [15:0]      expected_z_beats,
  input  logic             dma_data_valid,
  output logic             dma_data_ready,
  input  logic [1:0]       section,       // 0=q, 1=s, 2=z
  input  logic [127:0]     dma_data,
  output logic             all_loaded,
  output logic [15:0]      q_beats_loaded,
  output logic [15:0]      s_beats_loaded,
  output logic [15:0]      z_beats_loaded,
  output logic [MAX_Q_BEATS*128-1:0] q_bus,
  output logic [MAX_S_BEATS*128-1:0] s_bus,
  output logic [MAX_Z_BEATS*128-1:0] z_bus
);
endmodule
