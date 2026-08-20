// Fixed-point RMSNorm for the architecture's 1-centered Q2.13 norm weights.
//
// x uses Q*.14.  The accumulated mean-square is therefore Q*.28; isqrt()
// returns rms in Q*.14.  The reciprocal has Q*.14, then each channel applies
// gain=(1+norm_offset), where norm_offset is the model's signed Q2.13 value.
// This is a combinational functional unit; a performance implementation would
// pipeline sum-of-squares, reciprocal-sqrt and channel scaling.
module rmsnorm_q #(
  parameter int DIM          = 64,
  parameter int DATA_W       = 24,
  parameter int FRAC_W       = 14,
  parameter int NORM_W       = 16,
  parameter int NORM_FRAC_W  = 13,
  // round(1e-5 * 2^(2*14)) = 2684
  parameter logic [63:0] EPS_Q2FRAC = 64'd2684
) (
  input  logic signed [DIM*DATA_W-1:0] x_bus,
  input  logic signed [DIM*NORM_W-1:0] norm_offset_bus,
  output logic signed [DIM*DATA_W-1:0] y_bus
);
  import nano_arch_pkg::*;

  function automatic logic [31:0] isqrt64(input logic [63:0] value);
    logic [63:0] root;
    logic [63:0] trial;
    integer bit_index;
    begin
      root = '0;
      // Binary restoring equivalent: select root bits from MSB to LSB.
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
  logic signed [NORM_W-1:0] offset_value;
  logic signed [63:0] sum_square;
  logic [63:0] mean_square;
  logic [31:0] rms_qfrac;
  logic signed [63:0] inv_rms_qfrac;
  logic signed [63:0] normalized;
  logic signed [63:0] gain_q13;
  logic signed [63:0] result;

  always_comb begin
    sum_square = '0;
    for (i = 0; i < DIM; i = i + 1) begin
      x_value = $signed(x_bus[i*DATA_W +: DATA_W]);
      sum_square = sum_square + x_value * x_value;
    end
    mean_square = (sum_square / DIM) + EPS_Q2FRAC;
    rms_qfrac = isqrt64(mean_square);
    // (1<<28)/rms_q14 produces a reciprocal in Q*.14.
    if (rms_qfrac != 0)
      inv_rms_qfrac = (64'sd1 <<< (2*FRAC_W)) / $signed({1'b0, rms_qfrac});
    else
      inv_rms_qfrac = '0;

    y_bus = '0;
    for (i = 0; i < DIM; i = i + 1) begin
      x_value = $signed(x_bus[i*DATA_W +: DATA_W]);
      offset_value = $signed(norm_offset_bus[i*NORM_W +: NORM_W]);
      normalized = (x_value * inv_rms_qfrac) >>> FRAC_W;
      gain_q13 = (64'sd1 <<< NORM_FRAC_W) + offset_value;
      result = (normalized * gain_q13) >>> NORM_FRAC_W;
      y_bus[i*DATA_W +: DATA_W] = sat_data(result);
    end
  end
endmodule
