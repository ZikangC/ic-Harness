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
  // into a ~3-cycle hit after the first token.  Two ways absorb the few
  // low-hash collisions among the loaded tensors so the warm hit rate stays
  // ~100%.  Correctness is preserved: a hit re-reads the cached descriptor
  // and falls back to a full scan if the stored hash no longer matches, so a
  // stale or colliding entry can never return the wrong tensor.
  typedef enum logic [2:0] {IDLE, CACHE_READ, CACHE_RESPONSE, REQUEST, RESPONSE, DONE} state_t;
  state_t state;
  logic [31:0] index;
  localparam int CACHE_SETS = 512;
  logic cache_valid [0:1][0:CACHE_SETS-1];
  logic [31:0] cache_hash [0:1][0:CACHE_SETS-1];
  logic [15:0] cache_index [0:1][0:CACHE_SETS-1];
  integer w, ci;
  wire [8:0] cache_slot = wanted_name_hash[8:0];
  logic way0_hit, way1_hit;
  wire [15:0] hit_index = way0_hit ? cache_index[0][cache_slot] : cache_index[1][cache_slot];

  always_comb begin
    way0_hit = cache_valid[0][cache_slot] && (cache_hash[0][cache_slot] == wanted_name_hash);
    way1_hit = cache_valid[1][cache_slot] && (cache_hash[1][cache_slot] == wanted_name_hash);
    busy = (state == REQUEST) || (state == RESPONSE) ||
           (state == CACHE_READ) || (state == CACHE_RESPONSE);
    mem_req_valid = (state == REQUEST) || (state == CACHE_READ);
    mem_req_addr = descriptor_base + ({4'd0, index} << 4);
    mem_rsp_ready = (state == RESPONSE) || (state == CACHE_RESPONSE);
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= IDLE; index <= '0; found <= 1'b0; failed <= 1'b0;
      tensor_addr <= '0; tensor_bytes <= '0; tensor_aux <= '0;
      for (w = 0; w < 2; w = w + 1)
        for (ci = 0; ci < CACHE_SETS; ci = ci + 1) begin
          cache_valid[w][ci] <= 1'b0; cache_hash[w][ci] <= '0; cache_index[w][ci] <= '0;
        end
    end else begin
      case (state)
        IDLE: if (start) begin
          found <= 1'b0; failed <= 1'b0;
          if (way0_hit || way1_hit) begin
            index <= {16'd0, hit_index};
            state <= CACHE_READ;
          end else begin
            index <= '0;
            state <= REQUEST;
          end
        end
        CACHE_READ: state <= CACHE_RESPONSE;
        CACHE_RESPONSE: if (mem_rsp_valid) begin
          if (mem_rsp_rdata[31:0] == wanted_name_hash) begin
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
          if (mem_rsp_rdata[31:0] == wanted_name_hash) begin
            tensor_addr <= {4'd0, mem_rsp_rdata[63:32]};
            tensor_bytes <= mem_rsp_rdata[95:64];
            tensor_aux <= mem_rsp_rdata[127:96];
            found <= 1'b1;
            // Fill the first invalid way, else replace way 0.
            if (!cache_valid[0][cache_slot]) begin
              cache_valid[0][cache_slot] <= 1'b1; cache_hash[0][cache_slot] <= wanted_name_hash;
              cache_index[0][cache_slot] <= index[15:0];
            end else if (!cache_valid[1][cache_slot]) begin
              cache_valid[1][cache_slot] <= 1'b1; cache_hash[1][cache_slot] <= wanted_name_hash;
              cache_index[1][cache_slot] <= index[15:0];
            end else begin
              cache_hash[0][cache_slot] <= wanted_name_hash;
              cache_index[0][cache_slot] <= index[15:0];
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
        // A completed lookup re-arms in DONE.  Re-check the cache here so a
        // warm lookup (any section after the first token) takes the ~4-cycle
        // hit path instead of always falling through to a full linear scan.
        DONE: if (start) begin
          found <= 1'b0; failed <= 1'b0;
          if (way0_hit || way1_hit) begin
            index <= {16'd0, hit_index};
            state <= CACHE_READ;
          end else begin
            index <= '0;
            state <= REQUEST;
          end
        end
        default: state <= IDLE;
      endcase
    end
  end
endmodule
