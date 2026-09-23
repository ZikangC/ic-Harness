// Linear descriptor-table lookup. A descriptor is one 16-byte beat:
// {name_hash, byte_addr, byte_count, aux}. Linear search keeps this module
// independent of any private descriptor indexing scheme.
module descriptor_lookup (
  input  logic         clk,
  input  logic         rst_n,
  input  logic         start,
  input  logic [35:0]  descriptor_base,
  input  logic [31:0]  descriptor_count,
  input  logic [31:0]  wanted_name_hash,
  output logic         busy,
  output logic         found,
  output logic         failed,
  output logic [35:0]  tensor_addr,
  output logic [31:0]  tensor_bytes,
  output logic [31:0]  tensor_aux,
  output logic         mem_req_valid,
  input  logic         mem_req_ready,
  output logic [35:0]  mem_req_addr,
  input  logic         mem_rsp_valid,
  output logic         mem_rsp_ready,
  input  logic [127:0] mem_rsp_rdata
);
  // The descriptor table is static within one image and every tensor is
  // reloaded each token, so a small set-associative name_hash -> index cache
  // turns the ~246-entry linear scan (which dominated per-token latency)
  // into a ~4-cycle hit after the first token.  Two ways absorb the few
  // low-hash collisions among the loaded tensors so the warm hit rate stays
  // ~100%.  Correctness is preserved: a hit re-reads the cached descriptor
  // and falls back to a full scan if the stored hash no longer matches, so a
  // stale or colliding entry can never return the wrong tensor.
  //
  // The cache is read synchronously: `wanted_name_hash` is sampled into
  // `req_hash` on `start`, and the tag compare + index select are performed
  // in the LOOKUP state against that register.  Keeping the 512-entry select
  // off the live input port removes the non-physical async combinational
  // fan-in (input -> 512:1 tag/index mux) that the old always_comb read
  // synthesized; the lookup is now a normal register-to-register path.
  typedef enum logic [2:0] {IDLE, LOOKUP, CACHE_READ, CACHE_RESPONSE, REQUEST, RESPONSE, DONE} state_t;
  state_t state;
  logic [31:0] index;
  localparam int CACHE_SETS = 512;
  logic cache_valid [0:1][0:CACHE_SETS-1];
  logic [31:0] cache_hash [0:1][0:CACHE_SETS-1];
  logic [15:0] cache_index [0:1][0:CACHE_SETS-1];
  integer w, ci;
  logic [31:0] req_hash;

  always_comb begin
    busy = (state == REQUEST) || (state == RESPONSE) ||
           (state == CACHE_READ) || (state == CACHE_RESPONSE) ||
           (state == LOOKUP);
    mem_req_valid = (state == REQUEST) || (state == CACHE_READ);
    mem_req_addr = descriptor_base + ({4'd0, index} << 4);
    mem_rsp_ready = (state == RESPONSE) || (state == CACHE_RESPONSE);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE; index <= '0; found <= 1'b0; failed <= 1'b0;
      tensor_addr <= '0; tensor_bytes <= '0; tensor_aux <= '0; req_hash <= '0;
      for (w = 0; w < 2; w = w + 1)
        for (ci = 0; ci < CACHE_SETS; ci = ci + 1) begin
          cache_valid[w][ci] <= 1'b0; cache_hash[w][ci] <= '0; cache_index[w][ci] <= '0;
        end
    end else begin
      case (state)
        IDLE: if (start) begin
          found <= 1'b0; failed <= 1'b0;
          req_hash <= wanted_name_hash;
          state <= LOOKUP;
        end
        // Synchronous cache read: the array select comes from the sampled
        // req_hash flop, so the 512-entry tag/index mux is a register-to-
        // register path instead of an async fan-in from the module input.
        LOOKUP: begin
          if (cache_valid[0][req_hash[8:0]] && (cache_hash[0][req_hash[8:0]] == req_hash)) begin
            index <= {16'd0, cache_index[0][req_hash[8:0]]};
            state <= CACHE_READ;
          end else if (cache_valid[1][req_hash[8:0]] && (cache_hash[1][req_hash[8:0]] == req_hash)) begin
            index <= {16'd0, cache_index[1][req_hash[8:0]]};
            state <= CACHE_READ;
          end else begin
            index <= '0;
            state <= REQUEST;
          end
        end
        CACHE_READ: state <= CACHE_RESPONSE;
        CACHE_RESPONSE: if (mem_rsp_valid) begin
          if (mem_rsp_rdata[31:0] == req_hash) begin
            tensor_addr <= {4'd0, mem_rsp_rdata[63:32]};
            tensor_bytes <= mem_rsp_rdata[95:64];
            tensor_aux <= mem_rsp_rdata[127:96];
            found <= 1'b1;
            state <= DONE;
          end else begin
            // Stale cache entry (should not happen with a static image);
            // fall back to a full linear scan.
            index <= '0;
            state <= REQUEST;
          end
        end
        REQUEST: if (mem_req_ready) state <= RESPONSE;
        RESPONSE: if (mem_rsp_valid) begin
          if (mem_rsp_rdata[31:0] == req_hash) begin
            tensor_addr <= {4'd0, mem_rsp_rdata[63:32]};
            tensor_bytes <= mem_rsp_rdata[95:64];
            tensor_aux <= mem_rsp_rdata[127:96];
            found <= 1'b1;
            // Fill the first invalid way, else replace way 0.
            if (!cache_valid[0][req_hash[8:0]]) begin
              cache_valid[0][req_hash[8:0]] <= 1'b1; cache_hash[0][req_hash[8:0]] <= req_hash;
              cache_index[0][req_hash[8:0]] <= index[15:0];
            end else if (!cache_valid[1][req_hash[8:0]]) begin
              cache_valid[1][req_hash[8:0]] <= 1'b1; cache_hash[1][req_hash[8:0]] <= req_hash;
              cache_index[1][req_hash[8:0]] <= index[15:0];
            end else begin
              cache_hash[0][req_hash[8:0]] <= req_hash;
              cache_index[0][req_hash[8:0]] <= index[15:0];
            end
            state <= DONE;
          end else if (index + 1 >= descriptor_count) begin
            failed <= 1'b1;
            state <= DONE;
          end else begin
            index <= index + 32'd1;
            state <= REQUEST;
          end
        end
        // A completed lookup re-arms in DONE.  Re-sample the request and take
        // the synchronous cache path so a warm lookup (any section after the
        // first token) still uses the cache instead of a full linear scan.
        DONE: if (start) begin
          found <= 1'b0; failed <= 1'b0;
          req_hash <= wanted_name_hash;
          state <= LOOKUP;
        end
        default: state <= IDLE;
      endcase
    end
  end
endmodule
