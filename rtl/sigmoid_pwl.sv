// Shared sigmoid approximation. Input is signed Q*.14; output is unsigned
// Q1.14. Breakpoints are symmetric, monotonic PWL samples of sigmoid(x).
module sigmoid_pwl #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [DATA_W-1:0] x_in,
  output logic        [FRAC_W:0]   y_out
);
  logic signed [63:0] x;
  logic signed [63:0] y;
  localparam logic signed [63:0] ONE = 64'sd1 <<< FRAC_W;

  function automatic logic signed [63:0] lerp(
      input logic signed [63:0] x_value,
      input logic signed [63:0] x0,
      input logic signed [63:0] x1,
      input logic signed [63:0] y0,
      input logic signed [63:0] y1
  );
    lerp = y0 + ((x_value - x0) * (y1 - y0)) / (x1 - x0);
  endfunction

  always_comb begin
    x = x_in;
    if (x <= -8*ONE)       y = 9;
    else if (x <= -4*ONE)  y = lerp(x, -8*ONE, -4*ONE, 9, 295);
    else if (x <= -2*ONE)  y = lerp(x, -4*ONE, -2*ONE, 295, 1953);
    else if (x <= 0)       y = lerp(x, -2*ONE, 0, 1953, 8192);
    else if (x <= 2*ONE)   y = lerp(x, 0, 2*ONE, 8192, 14431);
    else if (x <= 4*ONE)   y = lerp(x, 2*ONE, 4*ONE, 14431, 16089);
    else if (x <= 8*ONE)   y = lerp(x, 4*ONE, 8*ONE, 16089, 16375);
    else                   y = ONE;
    y_out = y[FRAC_W:0];
  end
endmodule
