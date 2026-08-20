`timescale 1ns/1ps
// Transport-only regression: prove that public FNV descriptors load exactly
// the bytes the Python image generator put in emb.q / emb.s / emb.z.
module tb_qsz_tensor_loader;
  localparam int MEM_BYTES = 262144;
  localparam logic [31:0] EMB_Q = 32'h153aeda6;
  localparam logic [31:0] EMB_S = 32'h153aea40;
  localparam logic [31:0] EMB_Z = 32'h153af98b;
  logic clk = 0, rst_n = 0, start = 0, busy, loaded, error;
  logic [1024*128-1:0] q_bus;
  logic [64*128-1:0] s_bus, z_bus;
  logic d_req_v, d_req_r, d_rsp_v, d_rsp_r, x_req_v, x_req_r, x_rsp_v, x_rsp_r;
  logic [35:0] d_req_a, x_req_a;
  logic [127:0] d_rsp_d, x_rsp_d;
  logic [7:0] memory [0:MEM_BYTES-1];
  logic pending; logic pending_is_dma;
  logic [127:0] pending_data;
  logic [31:0] q_addr, s_addr, z_addr;
  integer i, cycles;
  string image_path;

  qsz_tensor_loader dut (
    .clk, .rst_n, .descriptor_base(36'd256), .descriptor_count(32'd246), .start, .busy, .loaded, .error,
    .q_name_hash(EMB_Q), .q_bytes(32'd16384), .s_name_hash(EMB_S), .s_bytes(32'd1024), .z_name_hash(EMB_Z), .z_bytes(32'd512),
    .q_bus, .s_bus, .z_bus,
    .desc_mem_req_valid(d_req_v), .desc_mem_req_ready(d_req_r), .desc_mem_req_addr(d_req_a),
    .desc_mem_rsp_valid(d_rsp_v), .desc_mem_rsp_ready(d_rsp_r), .desc_mem_rsp_rdata(d_rsp_d),
    .dma_mem_req_valid(x_req_v), .dma_mem_req_ready(x_req_r), .dma_mem_req_addr(x_req_a),
    .dma_mem_rsp_valid(x_rsp_v), .dma_mem_rsp_ready(x_rsp_r), .dma_mem_rsp_rdata(x_rsp_d)
  );
  always #5 clk = ~clk;
  assign d_req_r = !pending;
  assign x_req_r = !pending;
  assign d_rsp_v = pending && !pending_is_dma;
  assign x_rsp_v = pending && pending_is_dma;
  assign d_rsp_d = pending_data;
  assign x_rsp_d = pending_data;

  always_ff @(posedge clk) begin
    if (!rst_n) begin pending <= 0; pending_is_dma <= 0; pending_data <= '0; end
    else begin
      if (pending && ((!pending_is_dma && d_rsp_r) || (pending_is_dma && x_rsp_r))) pending <= 0;
      if (!pending && ((d_req_v && d_req_r) || (x_req_v && x_req_r))) begin
        pending_is_dma <= x_req_v;
        for (i = 0; i < 16; i = i + 1)
          pending_data[i*8 +: 8] <= memory[(x_req_v ? x_req_a : d_req_a) + i];
        pending <= 1;
      end
    end
  end

  initial begin
    if (!$value$plusargs("IMAGE=%s", image_path)) $fatal(1, "pass +IMAGE=<golden hex>");
    for (i = 0; i < MEM_BYTES; i = i + 1) memory[i] = '0;
    $readmemh(image_path, memory);
    q_addr = {memory[263],memory[262],memory[261],memory[260]};
    s_addr = {memory[279],memory[278],memory[277],memory[276]};
    z_addr = {memory[295],memory[294],memory[293],memory[292]};
    repeat (4) @(posedge clk); rst_n = 1;
    @(posedge clk); start = 1;
    @(posedge clk); start = 0;
    for (cycles = 0; cycles < 20000; cycles = cycles + 1) begin
      @(posedge clk);
      if (error) $fatal(1, "loader reported descriptor/DMA error");
      if (loaded) begin
        for (i = 0; i < 16; i = i + 1) begin
          if (q_bus[i*8 +: 8] !== memory[q_addr+i]) $fatal(1, "q byte %0d mismatch", i);
          if (s_bus[i*8 +: 8] !== memory[s_addr+i]) $fatal(1, "s byte %0d mismatch", i);
          if (z_bus[i*8 +: 8] !== memory[z_addr+i]) $fatal(1, "z byte %0d mismatch", i);
        end
        $display("PASS: public descriptor ABI loaded emb q/s/z exactly in %0d cycles", cycles);
        $finish;
      end
    end
    $fatal(1, "timeout");
  end
endmodule
