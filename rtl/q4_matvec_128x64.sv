// MLA wk_c / wv use a 128-dimensional normalized latent.  This dedicated
// datapath preserves the image's selectable group size: group=128 has one
// q/s/z group per output channel, while group=64 has two.
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
  logic signed [64*DATA_W-1:0] y_g128, y_g64;
  q4_dequant_matvec #(.IN(128), .OUT(64), .GROUP(128), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) one_group (
    .x_bus, .q_bus, .scale_bus(s_bus[64*16-1:0]), .zero_bus(z_bus[64*8-1:0]), .y_bus(y_g128));
  q4_dequant_matvec #(.IN(128), .OUT(64), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) two_groups (
    .x_bus, .q_bus, .scale_bus(s_bus), .zero_bus(z_bus), .y_bus(y_g64));
  always_comb y_bus = group_is_128 ? y_g128 : y_g64;
endmodule
