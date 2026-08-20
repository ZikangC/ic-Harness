// One complete SwiGLU expert: down(SiLU(h@g) elementwise (h@u)).
// All matrices use the task's standard int4 q/s/z layout and single group
// along each fan-in for the nano dimensions.
module swiglu_expert #(
  parameter int DATA_W = 24,
  parameter int FRAC_W = 14
) (
  input  logic signed [64*DATA_W-1:0] h_bus,
  input  logic [64/2*32*8-1:0]        g_q_bus,
  input  logic signed [32*16-1:0]     g_s_bus,
  input  logic [32*8-1:0]             g_z_bus,
  input  logic [64/2*32*8-1:0]        u_q_bus,
  input  logic signed [32*16-1:0]     u_s_bus,
  input  logic [32*8-1:0]             u_z_bus,
  input  logic [32/2*64*8-1:0]        d_q_bus,
  input  logic signed [64*16-1:0]     d_s_bus,
  input  logic [64*8-1:0]             d_z_bus,
  output logic signed [64*DATA_W-1:0] out_bus
);
  logic signed [32*DATA_W-1:0] gate_bus, up_bus, silu_gate_bus, mid_bus;
  genvar lane;

  q4_dequant_matvec #(.IN(64), .OUT(32), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) gate_mm (
    .x_bus(h_bus), .q_bus(g_q_bus), .scale_bus(g_s_bus), .zero_bus(g_z_bus), .y_bus(gate_bus)
  );
  q4_dequant_matvec #(.IN(64), .OUT(32), .GROUP(64), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) up_mm (
    .x_bus(h_bus), .q_bus(u_q_bus), .scale_bus(u_s_bus), .zero_bus(u_z_bus), .y_bus(up_bus)
  );
  generate
    for (lane = 0; lane < 32; lane = lane + 1) begin : g_silu
      silu_pwl #(.DATA_W(DATA_W), .FRAC_W(FRAC_W)) silu_unit (
        .x_in(gate_bus[lane*DATA_W +: DATA_W]),
        .y_out(silu_gate_bus[lane*DATA_W +: DATA_W])
      );
    end
  endgenerate
  integer i;
  logic signed [63:0] product;
  always_comb begin
    mid_bus = '0;
    for (i = 0; i < 32; i = i + 1) begin
      product = $signed(silu_gate_bus[i*DATA_W +: DATA_W]) * $signed(up_bus[i*DATA_W +: DATA_W]);
      mid_bus[i*DATA_W +: DATA_W] = product >>> FRAC_W;
    end
  end
  q4_dequant_matvec #(.IN(32), .OUT(64), .GROUP(32), .DATA_W(DATA_W), .FRAC_W(FRAC_W)) down_mm (
    .x_bus(mid_bus), .q_bus(d_q_bus), .scale_bus(d_s_bus), .zero_bus(d_z_bus), .y_bus(out_bus)
  );
endmodule
