// Storage-only MLA cache.  A separate attention MAC engine can stream an
// indexed K/V record from this module while accumulating q.K and p.V.
module mla_kv_cache #(
  parameter int MAX_SEQ = 64,
  parameter int HEADS   = 2,
  parameter int KD      = 48,
  parameter int DV      = 32,
  parameter int DATA_W  = 24
) (
  input  logic                               clk,
  input  logic                               rst_n,
  input  logic                               write_valid,
  input  logic [$clog2(MAX_SEQ)-1:0]         write_pos,
  input  logic signed [HEADS*KD*DATA_W-1:0]  write_key_bus,
  input  logic signed [HEADS*DV*DATA_W-1:0]  write_value_bus,
  input  logic [$clog2(MAX_SEQ)-1:0]         read_pos,
  output logic signed [HEADS*KD*DATA_W-1:0]  read_key_bus,
  output logic signed [HEADS*DV*DATA_W-1:0]  read_value_bus
);
  logic signed [DATA_W-1:0] key_mem   [0:MAX_SEQ-1][0:HEADS-1][0:KD-1];
  logic signed [DATA_W-1:0] value_mem [0:MAX_SEQ-1][0:HEADS-1][0:DV-1];
  integer head_read, dim_read;
  integer pos_reset, head_reset, dim_reset;
  integer head_write, dim_write;

  always_comb begin
    read_key_bus = '0;
    read_value_bus = '0;
    for (head_read = 0; head_read < HEADS; head_read = head_read + 1) begin
      for (dim_read = 0; dim_read < KD; dim_read = dim_read + 1)
        read_key_bus[(head_read*KD + dim_read)*DATA_W +: DATA_W] = key_mem[read_pos][head_read][dim_read];
      for (dim_read = 0; dim_read < DV; dim_read = dim_read + 1)
        read_value_bus[(head_read*DV + dim_read)*DATA_W +: DATA_W] = value_mem[read_pos][head_read][dim_read];
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (pos_reset = 0; pos_reset < MAX_SEQ; pos_reset = pos_reset + 1)
        for (head_reset = 0; head_reset < HEADS; head_reset = head_reset + 1) begin
          for (dim_reset = 0; dim_reset < KD; dim_reset = dim_reset + 1)
            key_mem[pos_reset][head_reset][dim_reset] <= '0;
          for (dim_reset = 0; dim_reset < DV; dim_reset = dim_reset + 1)
            value_mem[pos_reset][head_reset][dim_reset] <= '0;
        end
    end else if (write_valid) begin
      for (head_write = 0; head_write < HEADS; head_write = head_write + 1) begin
        for (dim_write = 0; dim_write < KD; dim_write = dim_write + 1)
          key_mem[write_pos][head_write][dim_write] <= write_key_bus[(head_write*KD + dim_write)*DATA_W +: DATA_W];
        for (dim_write = 0; dim_write < DV; dim_write = dim_write + 1)
          value_mem[write_pos][head_write][dim_write] <= write_value_bus[(head_write*DV + dim_write)*DATA_W +: DATA_W];
      end
    end
  end
endmodule
