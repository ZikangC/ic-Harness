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
  // Packed per-token rows.  Bit (h*KD+d)*DATA_W +: DATA_W of key_mem[t] is the
  // former (h,d) slice of the old 3D array, so the mapping is bit-identical;
  // each array now has exactly one write port and one read port.  The old
  // per-head/dim slicing became HEADS*(KD+DV) independent write ports in
  // Yosys `proc` and exploded MEMORY_BMUX2ROM/MEMORY_DFF under `memory -nomap`;
  // the packed form lets `memory_collect` close the `$mem` in one shot while
  // staying a `$mem` for SRAM macro accounting.
  logic signed [HEADS*KD*DATA_W-1:0] key_mem   [0:MAX_SEQ-1];
  logic signed [HEADS*DV*DATA_W-1:0] value_mem [0:MAX_SEQ-1];

  always_comb begin
    read_key_bus   = key_mem[read_pos];
    read_value_bus = value_mem[read_pos];
  end

  // Deliberately no reset on the storage arrays.  Every read position is
  // written before it is read (datapath adapter: ST_MCACHE writes the current
  // token before ST_MSTART scans [0..pos]; contract module: WRITE before
  // ATTEND), so stale power-up data is never observed.  Clearing all
  // MAX_SEQ*HEADS*(KD+DV) words on reset expands to a per-bit reset mux in
  // Yosys `proc` (PROC_MUX), which stalled synthesis; the arrays are instead
  // collected as SRAM ($mem) for the macro-accounting measurement.
  always_ff @(posedge clk) begin
    if (write_valid) begin
      key_mem[write_pos]   <= write_key_bus;
      value_mem[write_pos] <= write_value_bus;
    end
  end
endmodule
