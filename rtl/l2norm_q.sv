// KDA q/k L2 normalization: x / sqrt(sum(x^2) + 1e-6), no learned gain.
module l2norm_q #(
  parameter int DIM          = 32,
  parameter int DATA_W       = 24,
  parameter int FRAC_W       = 14,
  // round(1e-6 * 2^(2*14)) = 268
  parameter logic [63:0] EPS_Q2FRAC = 64'd268
) (
  input  logic signed [DIM*DATA_W-1:0] x_bus,
  output logic signed [DIM*DATA_W-1:0] y_bus
);
  import nano_arch_pkg::*;

  function automatic logic [31:0] isqrt64(input logic [63:0] value);
    logic [63:0] root;
    logic [63:0] trial;
    integer bit_index;
    begin
      root = '0;
      for (bit_index = 31; bit_index >= 0; bit_index = bit_index - 1) begin
        trial = root | (64'd1 << bit_index);
        if (trial * trial <= value)
          root = trial;
      end
      isqrt64 = root[31:0];
    end
  endfunction

  integer i;
  logic signed [DATA_W-1:0] x_value;
  logic signed [63:0] sum_square;
  logic [31:0] norm_qfrac;
  logic signed [63:0] inverse_qfrac;
  logic signed [63:0] result;

  always_comb begin
    sum_square = '0;
    for (i = 0; i < DIM; i = i + 1) begin
      x_value = $signed(x_bus[i*DATA_W +: DATA_W]);
      sum_square = sum_square + x_value * x_value;
    end
    norm_qfrac = isqrt64(sum_square + EPS_Q2FRAC);
    if (norm_qfrac != 0)
      inverse_qfrac = (64'sd1 <<< (2*FRAC_W)) / $signed({1'b0, norm_qfrac});
    else
      inverse_qfrac = '0;

    y_bus = '0;
    for (i = 0; i < DIM; i = i + 1) begin
      x_value = $signed(x_bus[i*DATA_W +: DATA_W]);
      result = (x_value * inverse_qfrac) >>> FRAC_W;
      y_bus[i*DATA_W +: DATA_W] = sat_data(result);
    end
  end
endmodule
