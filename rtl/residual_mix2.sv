// Vector blend once residual softmax probabilities have been generated.
// weight0 + weight1 is 1.0 in Q*.14.  Scores/norm/softmax can be scheduled
// separately to share the nonlinear unit with MLA softmax.
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
  integer i;
  logic signed [DATA_W-1:0] in0, in1;
  logic signed [63:0] sum;
  always_comb begin
    out_bus = '0;
    for (i = 0; i < DIM; i = i + 1) begin
      in0 = $signed(in0_bus[i*DATA_W +: DATA_W]);
      in1 = $signed(in1_bus[i*DATA_W +: DATA_W]);
      sum = in0 * weight0 + in1 * weight1;
      out_bus[i*DATA_W +: DATA_W] = sum >>> FRAC_W;
    end
  end
endmodule
