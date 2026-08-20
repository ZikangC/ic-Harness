// SiLU(x) = x * sigmoid(x), sharing the same Q1.14 sigmoid approximation.
module silu_pwl #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic signed [DATA_W-1:0] y_out
);
  logic [FRAC_W:0] sigmoid_value;
  logic signed [63:0] product;

  sigmoid_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) sigmoid_unit (
    .x_in(x_in), .y_out(sigmoid_value)
  );

  always_comb begin
    product = $signed(x_in) * $signed({1'b0, sigmoid_value});
    y_out = product >>> FRAC_W;
  end
endmodule
