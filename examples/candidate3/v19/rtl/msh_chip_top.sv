// ---------------------------------------------------------------------------
// msh_chip_top: spec §5 port list, thin wrapper around nano_core.
// Single clock, active-low synchronous reset effective on the rising edge.
// ---------------------------------------------------------------------------
module msh_chip_top (
  input  logic          clk,
  input  logic          rst_n,
  input  logic          cmd_valid,
  input  logic [31:0]   cmd_data,
  output logic          cmd_ready,
  output logic          rsp_valid,
  output logic [31:0]   rsp_data,
  input  logic          rsp_ready,
  output logic          mem_req_valid,
  output logic          mem_req_write,
  input  logic          mem_req_ready,
  output logic [35:0]   mem_req_addr,
  output logic [127:0]  mem_req_wdata,
  output logic [15:0]   mem_req_wstrb,
  input  logic          mem_rsp_valid,
  input  logic [127:0]  mem_rsp_rdata,
  output logic          mem_rsp_ready
);
  nano_core u_core (
    .clk(clk), .rst_n(rst_n),
    .cmd_valid(cmd_valid), .cmd_data(cmd_data), .cmd_ready(cmd_ready),
    .rsp_valid(rsp_valid), .rsp_data(rsp_data), .rsp_ready(rsp_ready),
    .mem_req_valid(mem_req_valid), .mem_req_write(mem_req_write),
    .mem_req_ready(mem_req_ready), .mem_req_addr(mem_req_addr),
    .mem_req_wdata(mem_req_wdata), .mem_req_wstrb(mem_req_wstrb),
    .mem_rsp_valid(mem_rsp_valid), .mem_rsp_rdata(mem_rsp_rdata),
    .mem_rsp_ready(mem_rsp_ready),
    .cyc_count(), .core_busy());
endmodule
