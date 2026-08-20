`timescale 1ns/1ps
module tb_nano_decoder_top;
  localparam int MEM_BYTES = 262144;
  localparam int MAX_CYCLES = 500000;
  logic clk = 1'b0, rst_n = 1'b0, run = 1'b0;
  logic busy, done, error;
  logic mem_req_valid, mem_req_ready, mem_req_write;
  logic [35:0] mem_req_addr;
  logic [127:0] mem_req_wdata;
  logic [15:0] mem_req_wstrb;
  logic mem_rsp_valid, mem_rsp_ready;
  logic [127:0] mem_rsp_rdata;
  logic [7:0] memory [0:MEM_BYTES-1];
  logic rsp_pending, saw_status;
  integer cycles, i;
  string image_path;
  logic [35:0] status_addr;

  nano_decoder_top dut (
    .clk, .rst_n, .run, .busy, .done, .error,
    .mem_req_valid, .mem_req_ready, .mem_req_write, .mem_req_addr,
    .mem_req_wdata, .mem_req_wstrb, .mem_rsp_valid, .mem_rsp_ready, .mem_rsp_rdata
  );
  always #5 clk = ~clk;
  assign mem_req_ready = !rsp_pending;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      mem_rsp_valid <= 1'b0; mem_rsp_rdata <= '0; rsp_pending <= 1'b0; saw_status <= 1'b0;
    end else begin
      mem_rsp_valid <= 1'b0;
      if (rsp_pending && mem_rsp_ready) rsp_pending <= 1'b0;
      if (mem_req_valid && mem_req_ready) begin
        if (mem_req_write) begin
          for (i = 0; i < 16; i = i + 1)
            if (mem_req_wstrb[i]) memory[mem_req_addr + i] <= mem_req_wdata[i*8 +: 8];
          if (mem_req_addr == status_addr && mem_req_wdata[31:0] == 32'h600d_d00d) saw_status <= 1'b1;
        end else begin
          for (i = 0; i < 16; i = i + 1)
            mem_rsp_rdata[i*8 +: 8] <= memory[mem_req_addr + i];
          rsp_pending <= 1'b1;
        end
      end
      if (rsp_pending) mem_rsp_valid <= 1'b1;
    end
  end

  initial begin
    if (!$value$plusargs("IMAGE=%s", image_path)) image_path = "tb/smoke_image.hex";
    for (i = 0; i < MEM_BYTES; i = i + 1) memory[i] = '0;
    $readmemh(image_path, memory);
    status_addr = {memory[111], memory[110], memory[109], memory[108]};
    repeat (5) @(posedge clk);
    rst_n = 1'b1;
    @(posedge clk); run = 1'b1;
    @(posedge clk); run = 1'b0;
    for (cycles = 0; cycles < MAX_CYCLES; cycles = cycles + 1) begin
      @(posedge clk);
      if (error) $fatal(1, "top error at cycle %0d: stream_state=%0d hash=0x%08x op=%0d tensor=%0d expert=%0d section=%0d desc_found=%0b desc_failed=%0b expected_bytes=%0d desc_bytes=%0d",
                        cycles, dut.stream.state, dut.stream.wanted_hash, dut.stream.active_op,
                        dut.stream.active_tensor, dut.stream.active_expert, dut.stream.active_section,
                        dut.stream.desc_found, dut.stream.desc_failed, dut.stream.expected_bytes, dut.stream.desc_tensor_bytes);
      if (done) begin
        if (!saw_status) $fatal(1, "done without final status write");
        $display("PASS: run -> done in %0d cycles, status=0x%08x", cycles,
                 {memory[status_addr+3],memory[status_addr+2],memory[status_addr+1],memory[status_addr]});
        $finish;
      end
    end
    $fatal(1, "timeout after %0d cycles", MAX_CYCLES);
  end
endmodule
