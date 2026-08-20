// Fused int4(q/s/z) dequantization and one vector-by-matrix multiply.
// q is packed along IN: low nibble is input 2*r, high nibble input 2*r+1.
module q4_dequant_matvec #(
  parameter int IN       = 64,
  parameter int OUT      = 64,
  parameter int GROUP    = 64,
  parameter int DATA_W   = 24,
  parameter int FRAC_W   = 14,
  localparam int GROUPS  = (IN + GROUP - 1) / GROUP
) (
  input  logic signed [IN*DATA_W-1:0]          x_bus,
  input  logic        [(IN/2)*OUT*8-1:0]       q_bus,
  input  logic signed [GROUPS*OUT*16-1:0]      scale_bus, // Q3.12
  // Standard image ABI stores each zero point as one uint8; only its low
  // nibble is meaningful.  Do not compact this stream in the compute path.
  input  logic        [GROUPS*OUT*8-1:0]       zero_bus,
  output logic signed [OUT*DATA_W-1:0]         y_bus
);
  integer i, j, group_index;
  logic [7:0] packed_byte;
  logic [3:0] q4;
  logic [3:0] zero_point;
  logic signed [15:0] scale_q12;
  logic signed [DATA_W-1:0] x_value;
  logic signed [63:0] accumulator;
  logic signed [63:0] weight_q12;
  integer signed q_delta;

  always_comb begin
    y_bus = '0;
    for (j = 0; j < OUT; j = j + 1) begin
      accumulator = 0;
      for (i = 0; i < IN; i = i + 1) begin
        packed_byte = q_bus[((i / 2) * OUT + j) * 8 +: 8];
        q4 = (i[0] == 1'b0) ? packed_byte[3:0] : packed_byte[7:4];
        group_index = i / GROUP;
        zero_point = zero_bus[(group_index * OUT + j) * 8 +: 4];
        scale_q12 = $signed(scale_bus[(group_index * OUT + j) * 16 +: 16]);
        // weight_q12 represents (q-z)*scale, therefore has 12 fractional bits.
        q_delta = $signed({1'b0, q4}) - $signed({1'b0, zero_point});
        weight_q12 = q_delta * scale_q12;
        x_value = $signed(x_bus[i*DATA_W +: DATA_W]);
        accumulator = accumulator + x_value * weight_q12;
      end
      // x is Q*.14 and weight is Q*.12; restore a Q*.14 output.
      y_bus[j*DATA_W +: DATA_W] = accumulator >>> 12;
    end
  end
endmodule
