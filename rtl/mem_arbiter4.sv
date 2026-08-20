// Four-master arbiter for the single 16-byte memory port. It permits one
// outstanding read at a time, which keeps response ownership unambiguous and
// is latency-elastic. Masters 0..2 are intended for header/descriptor/DMA;
// master 3 is the logit writer.
module mem_arbiter4 (
  input  logic clk, input logic rst_n,
  input logic m0_req_valid, input logic m0_req_write, input logic [35:0] m0_req_addr,
  input logic [127:0] m0_req_wdata, input logic [15:0] m0_req_wstrb, output logic m0_req_ready,
  output logic m0_rsp_valid, input logic m0_rsp_ready, output logic [127:0] m0_rsp_rdata,
  input logic m1_req_valid, input logic m1_req_write, input logic [35:0] m1_req_addr,
  input logic [127:0] m1_req_wdata, input logic [15:0] m1_req_wstrb, output logic m1_req_ready,
  output logic m1_rsp_valid, input logic m1_rsp_ready, output logic [127:0] m1_rsp_rdata,
  input logic m2_req_valid, input logic m2_req_write, input logic [35:0] m2_req_addr,
  input logic [127:0] m2_req_wdata, input logic [15:0] m2_req_wstrb, output logic m2_req_ready,
  output logic m2_rsp_valid, input logic m2_rsp_ready, output logic [127:0] m2_rsp_rdata,
  input logic m3_req_valid, input logic m3_req_write, input logic [35:0] m3_req_addr,
  input logic [127:0] m3_req_wdata, input logic [15:0] m3_req_wstrb, output logic m3_req_ready,
  output logic m3_rsp_valid, input logic m3_rsp_ready, output logic [127:0] m3_rsp_rdata,
  output logic mem_req_valid, input logic mem_req_ready, output logic mem_req_write,
  output logic [35:0] mem_req_addr, output logic [127:0] mem_req_wdata,
  output logic [15:0] mem_req_wstrb, input logic mem_rsp_valid,
  output logic mem_rsp_ready, input logic [127:0] mem_rsp_rdata
);
  logic read_pending;
  logic [1:0] read_owner, selected_owner;
  logic selected_valid, selected_write;
  logic [35:0] selected_addr;
  logic [127:0] selected_wdata;
  logic [15:0] selected_wstrb;

  always_comb begin
    selected_valid = 1'b0; selected_owner = 2'd0; selected_write = 1'b0;
    selected_addr = '0; selected_wdata = '0; selected_wstrb = '0;
    // Fixed priority is sufficient for the functional baseline. DMA should
    // become round-robin/prefetching only after correctness is established.
    if (m0_req_valid) begin
      selected_valid=1'b1; selected_owner=2'd0; selected_write=m0_req_write;
      selected_addr=m0_req_addr; selected_wdata=m0_req_wdata; selected_wstrb=m0_req_wstrb;
    end else if (m1_req_valid) begin
      selected_valid=1'b1; selected_owner=2'd1; selected_write=m1_req_write;
      selected_addr=m1_req_addr; selected_wdata=m1_req_wdata; selected_wstrb=m1_req_wstrb;
    end else if (m2_req_valid) begin
      selected_valid=1'b1; selected_owner=2'd2; selected_write=m2_req_write;
      selected_addr=m2_req_addr; selected_wdata=m2_req_wdata; selected_wstrb=m2_req_wstrb;
    end else if (m3_req_valid) begin
      selected_valid=1'b1; selected_owner=2'd3; selected_write=m3_req_write;
      selected_addr=m3_req_addr; selected_wdata=m3_req_wdata; selected_wstrb=m3_req_wstrb;
    end

    mem_req_valid = selected_valid && !read_pending;
    mem_req_write = selected_write;
    mem_req_addr = selected_addr;
    mem_req_wdata = selected_wdata;
    mem_req_wstrb = selected_wstrb;
    m0_req_ready = mem_req_ready && mem_req_valid && selected_owner == 2'd0;
    m1_req_ready = mem_req_ready && mem_req_valid && selected_owner == 2'd1;
    m2_req_ready = mem_req_ready && mem_req_valid && selected_owner == 2'd2;
    m3_req_ready = mem_req_ready && mem_req_valid && selected_owner == 2'd3;

    m0_rsp_valid = 1'b0; m1_rsp_valid = 1'b0; m2_rsp_valid = 1'b0; m3_rsp_valid = 1'b0;
    m0_rsp_rdata = mem_rsp_rdata; m1_rsp_rdata = mem_rsp_rdata;
    m2_rsp_rdata = mem_rsp_rdata; m3_rsp_rdata = mem_rsp_rdata;
    mem_rsp_ready = 1'b0;
    if (read_pending) begin
      case (read_owner)
        2'd0: begin m0_rsp_valid=mem_rsp_valid; mem_rsp_ready=m0_rsp_ready; end
        2'd1: begin m1_rsp_valid=mem_rsp_valid; mem_rsp_ready=m1_rsp_ready; end
        2'd2: begin m2_rsp_valid=mem_rsp_valid; mem_rsp_ready=m2_rsp_ready; end
        2'd3: begin m3_rsp_valid=mem_rsp_valid; mem_rsp_ready=m3_rsp_ready; end
      endcase
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin read_pending <= 1'b0; read_owner <= '0; end
    else begin
      if (!read_pending && mem_req_valid && mem_req_ready && !selected_write) begin
        read_pending <= 1'b1;
        read_owner <= selected_owner;
      end else if (read_pending && mem_rsp_valid && mem_rsp_ready) begin
        read_pending <= 1'b0;
      end
    end
  end
endmodule
