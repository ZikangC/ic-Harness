// Untied int4 embedding lookup.  The streamed q/s/z buffers hold all 512
// rows: q[512,32] bytes, s[512] int16 Q*.12, z[512] uint8 (low nibble used).
// A row becomes a Q*.14 activation vector, so it can enter RMSNorm directly.
module q4_embedding_lookup #(
  parameter int VOCAB  = 512,
  parameter int DIM    = 64,
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic [$clog2(VOCAB)-1:0] token_id,
  input  logic [VOCAB*(DIM/2)*8-1:0] q_bus,
  input  logic signed [VOCAB*16-1:0] scale_bus,
  // `emb.z` is one byte per vocabulary row in the public image ABI.
  input  logic [VOCAB*8-1:0] zero_bus,
  output logic signed [DIM*DATA_W-1:0] embedding_bus
);
  integer d;
  logic [7:0] packed_byte;
  logic [3:0] q4, zero_point;
  logic signed [15:0] scale_q12;
  // Narrowed from the full 32-bit integer delta / 64-bit product: q4 and
  // zero_point are 4-bit nibbles, so delta = q4 - zero_point has real range
  // [-15, 15].  The former wide types made every one of the DIM=64 multiplies
  // expand as 32x16->64 in Yosys techmap (stuck at pass 61.2).  6-bit signed
  // delta and a parameter-derived PRODUCT_W keep the exact value range, so the
  // low DATA_W output bits stay bit-identical to the old 64-bit datapath.
  localparam int PRODUCT_W = 6 + 16 + ((FRAC_W >= 12) ? FRAC_W - 12 : 0);
  logic signed [PRODUCT_W-1:0] product;
  logic signed [5:0] delta;

  always_comb begin
    embedding_bus = '0;
    scale_q12 = $signed(scale_bus[token_id*16 +: 16]);
    zero_point = zero_bus[token_id*8 +: 4];
    for (d = 0; d < DIM; d = d + 1) begin
      packed_byte = q_bus[(token_id*(DIM/2) + d/2)*8 +: 8];
      q4 = d[0] ? packed_byte[7:4] : packed_byte[3:0];
      delta = $signed({1'b0, q4}) - $signed({1'b0, zero_point});
      // q4*scale is Q*.12. Shift left by two to make the shared Q*.14 format.
      product = delta * scale_q12;
      embedding_bus[d*DATA_W +: DATA_W] = product <<< (FRAC_W - 12);
    end
  end
endmodule
