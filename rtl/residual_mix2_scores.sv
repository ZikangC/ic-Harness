// Two-way residual softmax, using softmax([s0,s1]) =
// [1-sigmoid(s1-s0), sigmoid(s1-s0)]. This produces Q1.14 weights for the
// vector blend module and shares the sigmoid PWL unit with KDA/MoE gates.
module residual_mix2_scores #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] score0,
  input  logic signed [DATA_W-1:0] score1,
  output logic        [FRAC_W:0]   weight0,
  output logic        [FRAC_W:0]   weight1
);
  logic signed [DATA_W-1:0] difference;
  logic [FRAC_W:0] sigmoid_difference;

  sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sigmoid_unit (
    .x_in(difference), .y_out(sigmoid_difference)
  );

  always_comb begin
    difference = score1 - score0;
    weight1 = sigmoid_difference;
    weight0 = (1 << FRAC_W) - sigmoid_difference;
  end
endmodule
