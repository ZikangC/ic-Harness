// Reusable on-chip buffer for one streamed int4 matrix and its q/s/z side
// data. The largest nano tensor is the LM head: 1024 q beats, 64 scale beats,
// 16 zero-point beats. A serialized adapter reuses this buffer for each
// projection instead of storing every model tensor on chip.
module matrix_qsz_buffer #(
  parameter int MAX_Q_BEATS = 1024,
  parameter int MAX_S_BEATS = 64,
  parameter int MAX_Z_BEATS = 64
) (
  input  logic             clk,
  input  logic             rst_n,
  input  logic             begin_load,
  input  logic [15:0]      expected_q_beats,
  input  logic [15:0]      expected_s_beats,
  input  logic [15:0]      expected_z_beats,
  // dma_data_valid is the beat stream from dma_read16. section selects
  // which tensor of a q/s/z triple is currently being transferred.
  input  logic             dma_data_valid,
  output logic             dma_data_ready,
  input  logic [1:0]       section,       // 0=q, 1=s, 2=z
  input  logic [127:0]     dma_data,
  output logic             all_loaded,
  output logic [15:0]      q_beats_loaded,
  output logic [15:0]      s_beats_loaded,
  output logic [15:0]      z_beats_loaded,
  output logic [MAX_Q_BEATS*128-1:0] q_bus,
  output logic [MAX_S_BEATS*128-1:0] s_bus,
  output logic [MAX_Z_BEATS*128-1:0] z_bus
);
  logic [127:0] q_mem [0:MAX_Q_BEATS-1];
  logic [127:0] s_mem [0:MAX_S_BEATS-1];
  logic [127:0] z_mem [0:MAX_Z_BEATS-1];
  logic [15:0] expected_q, expected_s, expected_z;
  integer i;

  always_comb begin
    dma_data_ready = 1'b0;
    case (section)
      2'd0: dma_data_ready = (q_beats_loaded < expected_q) && (q_beats_loaded < MAX_Q_BEATS);
      2'd1: dma_data_ready = (s_beats_loaded < expected_s) && (s_beats_loaded < MAX_S_BEATS);
      2'd2: dma_data_ready = (z_beats_loaded < expected_z) && (z_beats_loaded < MAX_Z_BEATS);
      default: ;
    endcase
    all_loaded = (q_beats_loaded == expected_q) &&
                 (s_beats_loaded == expected_s) &&
                 (z_beats_loaded == expected_z);
    q_bus = '0; s_bus = '0; z_bus = '0;
    for (i = 0; i < MAX_Q_BEATS; i = i + 1) q_bus[i*128 +: 128] = q_mem[i];
    for (i = 0; i < MAX_S_BEATS; i = i + 1) s_bus[i*128 +: 128] = s_mem[i];
    for (i = 0; i < MAX_Z_BEATS; i = i + 1) z_bus[i*128 +: 128] = z_mem[i];
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      q_beats_loaded <= '0; s_beats_loaded <= '0; z_beats_loaded <= '0;
      expected_q <= '0; expected_s <= '0; expected_z <= '0;
    end else if (begin_load) begin
      q_beats_loaded <= '0; s_beats_loaded <= '0; z_beats_loaded <= '0;
      expected_q <= expected_q_beats;
      expected_s <= expected_s_beats;
      expected_z <= expected_z_beats;
    end else if (dma_data_valid && dma_data_ready) begin
      case (section)
        2'd0: begin q_mem[q_beats_loaded] <= dma_data; q_beats_loaded <= q_beats_loaded + 1'b1; end
        2'd1: begin s_mem[s_beats_loaded] <= dma_data; s_beats_loaded <= s_beats_loaded + 1'b1; end
        2'd2: begin z_mem[z_beats_loaded] <= dma_data; z_beats_loaded <= z_beats_loaded + 1'b1; end
        default: ;
      endcase
    end
  end
endmodule
