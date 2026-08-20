// Contract-accurate two-candidate residual mixer.
//
// For nano's residual block size of two, candidates are the saved block
// boundary and the current prefix.  Each candidate is independently
// RMS-normalized with the same `res*.norm.w`, scored by `res*.proj`, then
// softmaxed before blending the *unnormalized* candidates.
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
  logic signed [DIM*DATA_W-1:0] block_norm, prefix_norm;
  logic [FRAC_W:0] w0, w1;
  logic signed [63:0] accum0, accum1;
  integer i;

  rmsnorm_q #(.DIM(DIM), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) norm_block
    (.x_bus(block_bus), .norm_offset_bus, .y_bus(block_norm));
  rmsnorm_q #(.DIM(DIM), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) norm_prefix
    (.x_bus(prefix_bus), .norm_offset_bus, .y_bus(prefix_norm));
  residual_mix2_scores #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) choose
    (.score0(block_score), .score1(prefix_score), .weight0(w0), .weight1(w1));
  residual_mix2 #(.DIM(DIM), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) blend
    (.in0_bus(block_bus), .in1_bus(prefix_bus), .weight0(w0), .weight1(w1), .out_bus);

  always_comb begin
    accum0 = 0; accum1 = 0;
    for (i = 0; i < DIM; i = i + 1) begin
      accum0 = accum0 + $signed(block_norm[i*DATA_W +: DATA_W]) * $signed(proj_bus[i*16 +: 16]);
      accum1 = accum1 + $signed(prefix_norm[i*DATA_W +: DATA_W]) * $signed(proj_bus[i*16 +: 16]);
    end
    // norm is Q*.14, projection Q*.11, so scores are restored to Q*.14.
    block_score = accum0 >>> 11;
    prefix_score = accum1 >>> 11;
  end
endmodule
