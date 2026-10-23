// External memory interface: one read request presented per cycle with a hold
// register, an ordered read-response FIFO, and one posted-write slot.
// The chip never assumes a fixed response latency: transfers obey valid/ready
// only, payload is held while valid=1 && ready=0, and every accepted response
// is staged in the FIFO so it is never lost.
//
// Both request paths are *combinational* from the requester to the external
// port, with a hold register that captures the payload only when the port did
// not accept it in the presentation cycle.  A registered request slot would
// serialize one request every two cycles (the slot is only freed one cycle
// after it is launched), which halves the read bandwidth of the weight-stream
// loader and the posted-write drain rate.
//
// Two independent counters are kept:
//   inflight : read requests accepted by the external memory but not answered
//   occ      : read responses currently held in the FIFO (fifo_valid = occ != 0)
// The read-request credit uses inflight+occ, so the FIFO can always hold every
// outstanding response and a consumer can never pop an empty FIFO.
module nano_mem #(
  parameter int FDEPTH = 32,
  parameter int FAW    = 5                 // log2(FDEPTH)
) (
  input  logic          clk,
  input  logic          rst_n,
  // read request side
  input  logic          rd_issue,
  input  logic [35:0]   rd_addr,
  output logic          rd_ready,          // request accepted (may then be held)
  // ordered response side
  output logic          fifo_valid,
  input  logic          fifo_pop,          // combinational pop strobe
  output logic [127:0]  fifo_data,
  // posted write side
  input  logic          wr_issue,
  input  logic [35:0]   wr_addr,
  input  logic [127:0]  wr_data,
  input  logic [15:0]   wr_strb,
  output logic          wr_ready,          // write accepted (may then be held)
  // external memory
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
  localparam int CNTW = FAW + 1;

  logic [127:0]    fifo_mem [FDEPTH];
  logic [FAW-1:0]  wr_ptr, rd_ptr;
  logic [CNTW-1:0] occ, inflight;

  // held (presented but not yet accepted) requests
  logic          rd_h;
  logic [35:0]   rd_h_addr;
  logic          wr_h;
  logic [35:0]   wr_h_addr;
  logic [127:0]  wr_h_data;
  logic [15:0]   wr_h_strb;

  logic rd_new, rd_present, rd_acc, wr_new, wr_present, wr_acc;
  logic [35:0]  rd_present_addr, wr_present_addr;
  logic [127:0] wr_present_data;
  logic [15:0]  wr_present_strb;
  logic pop, push;
  logic [CNTW-1:0] nxt_occ, nxt_inf;

  // A read request is accepted (and the requester may advance) only when no
  // other read or write request is held and a response slot is reserved.
  assign rd_new          = rd_issue && !rd_h && !wr_h &&
                           ((occ + inflight) < CNTW'(FDEPTH));
  assign rd_ready        = rd_new;
  assign rd_present      = (rd_h || rd_new) && !wr_h;
  assign rd_present_addr = rd_h ? rd_h_addr : rd_addr;
  assign rd_acc          = rd_present && mem_req_ready;

  // A posted write is accepted only when no read request is held or presented.
  assign wr_new          = wr_issue && !wr_h && !rd_h && !rd_new;
  assign wr_ready        = wr_new;
  assign wr_present      = (wr_h || wr_new) && !rd_h && !rd_new;
  assign wr_present_addr = wr_h ? wr_h_addr : wr_addr;
  assign wr_present_data = wr_h ? wr_h_data : wr_data;
  assign wr_present_strb = wr_h ? wr_h_strb : wr_strb;
  assign wr_acc          = wr_present && mem_req_ready;

  assign fifo_valid = (occ != 0);
  assign fifo_data  = fifo_mem[rd_ptr];
  assign pop        = fifo_pop && fifo_valid;
  assign push       = mem_rsp_valid && mem_rsp_ready;

  assign nxt_occ = occ + (push ? CNTW'(1) : CNTW'(0)) - (pop ? CNTW'(1) : CNTW'(0));
  assign nxt_inf = inflight + (rd_acc ? CNTW'(1) : CNTW'(0)) - (push ? CNTW'(1) : CNTW'(0));

  // Spec §5: active-low synchronous reset (sampled on the rising edge).
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      rd_h      <= 1'b0;
      wr_h      <= 1'b0;
      occ       <= '0;
      inflight  <= '0;
      wr_ptr    <= '0;
      rd_ptr    <= '0;
      rd_h_addr <= '0;
      wr_h_addr <= '0;
      wr_h_data <= '0;
      wr_h_strb <= '0;
    end else begin
      if (rd_acc) rd_h <= 1'b0;
      if (rd_new && !rd_acc) begin
        rd_h      <= 1'b1;
        rd_h_addr <= rd_addr;
      end

      if (wr_acc) wr_h <= 1'b0;
      if (wr_new && !wr_acc) begin
        wr_h      <= 1'b1;
        wr_h_addr <= wr_addr;
        wr_h_data <= wr_data;
        wr_h_strb <= wr_strb;
      end

      if (push) begin
        fifo_mem[wr_ptr] <= mem_rsp_rdata;
        wr_ptr <= wr_ptr + FAW'(1);
      end
      if (pop) rd_ptr <= rd_ptr + FAW'(1);

      occ      <= nxt_occ;
      inflight <= nxt_inf;
    end
  end

  always_comb begin
    if (wr_present) begin
      mem_req_valid = 1'b1;
      mem_req_write = 1'b1;
      mem_req_addr  = wr_present_addr;
      mem_req_wdata = wr_present_data;
      mem_req_wstrb = wr_present_strb;
    end else if (rd_present) begin
      mem_req_valid = 1'b1;
      mem_req_write = 1'b0;
      mem_req_addr  = rd_present_addr;
      mem_req_wdata = 128'd0;
      mem_req_wstrb = 16'd0;
    end else begin
      mem_req_valid = 1'b0;
      mem_req_write = 1'b0;
      mem_req_addr  = 36'd0;
      mem_req_wdata = 128'd0;
      mem_req_wstrb = 16'd0;
    end
    mem_rsp_ready = 1'b1;
  end
endmodule
