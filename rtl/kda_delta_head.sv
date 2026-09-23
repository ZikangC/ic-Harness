// One 32-dimensional KDA head.  q_in and k_in must already have L2 norm;
// alpha is a per-key-channel decay in Q*.14 and beta is a scalar Q*.14.
//
// Round-27 (measurement completion, RTL structure fix): the previous single
// always_comb computed all 32 columns in one module -- ~4096 24x24 MACs whose
// Yosys techmap netlist exploded to ~19M cells and never reached ABC.  The
// computation has NO cross-column data dependency (each column re-zeroes its
// own sum; decay uses per-row alpha; delta and readout touch only that
// column's state), so it is split bit-identically into DH parameterized
// kda_delta_col instances.  The port list, parameterization, latency and FSM
// contract are unchanged; only the module structure changed so the measure
// flow can map the single-column body (leaf pass) and this wrapper (top pass)
// in bounded ABC windows.
module kda_delta_head #(
  parameter int DH      = 32,
  parameter int DATA_W  = 24,
  parameter int FRAC_W  = 14
) (
  input  logic                         clk,
  input  logic                         rst_n,
  input  logic                         step_valid,
  input  logic signed [DH*DATA_W-1:0]  q_in,
  input  logic signed [DH*DATA_W-1:0]  k_in,
  input  logic signed [DH*DATA_W-1:0]  v_in,
  input  logic signed [DH*DATA_W-1:0]  alpha_in,
  input  logic signed [DATA_W-1:0]     beta_in,
  output logic signed [DH*DATA_W-1:0]  out_bus
);
  genvar col;
  generate
    for (col = 0; col < DH; col = col + 1) begin : g_col
      kda_delta_col #(
        .DH(DH),
        .DATA_W(DATA_W),
        .FRAC_W(FRAC_W)
      ) u_col (
        .clk(clk),
        .rst_n(rst_n),
        .step_valid(step_valid),
        .q_in(q_in),
        .k_in(k_in),
        .v_in(v_in[col*DATA_W +: DATA_W]),
        .alpha_in(alpha_in),
        .beta_in(beta_in),
        .out_bus(out_bus[col*DATA_W +: DATA_W])
      );
    end
  endgenerate
endmodule
