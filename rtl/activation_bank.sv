// Small vector register file for the current token. Slots are assigned by the
// datapath adapter: x0, current x, prefix, attention, residual mix, MoE and
// final hidden state. KDA state and MLA cache are held in their dedicated
// modules because they survive across token positions.
module activation_bank #(
  parameter int SLOTS  = 8,
  parameter int DIM    = 64,
  parameter int DATA_W = 24,
  localparam int SLOT_W = $clog2(SLOTS)
) (
  input  logic                         clk,
  input  logic                         rst_n,
  input  logic                         write_valid,
  input  logic [SLOT_W-1:0]            write_slot,
  input  logic signed [DIM*DATA_W-1:0] write_bus,
  input  logic [SLOT_W-1:0]            read_slot0,
  output logic signed [DIM*DATA_W-1:0] read_bus0,
  input  logic [SLOT_W-1:0]            read_slot1,
  output logic signed [DIM*DATA_W-1:0] read_bus1
);
  logic signed [DATA_W-1:0] storage [0:SLOTS-1][0:DIM-1];
  integer i;

  always_comb begin
    read_bus0 = '0;
    read_bus1 = '0;
    for (i = 0; i < DIM; i = i + 1) begin
      read_bus0[i*DATA_W +: DATA_W] = storage[read_slot0][i];
      read_bus1[i*DATA_W +: DATA_W] = storage[read_slot1][i];
    end
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      for (i = 0; i < SLOTS*DIM; i = i + 1)
        storage[i / DIM][i % DIM] <= '0;
    end else if (write_valid) begin
      for (i = 0; i < DIM; i = i + 1)
        storage[write_slot][i] <= write_bus[i*DATA_W +: DATA_W];
    end
  end
endmodule
