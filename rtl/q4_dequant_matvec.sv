// Per-output-channel-block tap-accumulation leaf of the fused int4(q/s/z)
// dequantize + vector-by-matrix product.  One leaf computes a contiguous
// block of NCHAN columns (CHAN_BASE .. CHAN_BASE+NCHAN-1) of the product, so
// a single module's genrtlil body is O(NCHAN*IN) instead of O(IN*OUT).
// Arithmetic and >>>12 rounding are identical to the flat always_comb
// implementation; (CHAN_BASE+c) replaces the former outer loop variable j.
//
// Structural change (Round-12): the previous body expanded one IN*NCHAN array
// of (q-z)*scale -> x*weight (64x24-bit) multipliers plus a 45-bit adder tree
// per channel, which stalled ABC mapping on the IN=128 probe.  This body
// factors the dot product algebraically for each group g and channel c:
//
//   acc[c] = SUM_g scale[g,c] * ( S_xq[g,c] - z[g,c] * S_x[g] )
//   S_xq[g,c] = SUM_{i in g} x[i] * q4[i,c]       (24x4-bit products)
//   S_x[g]    = SUM_{i in g} x[i]                 (shared across channels)
//
// This is bit-identical to SUM_i x[i]*(q4[i,c]-z[g,c])*scale[g,c]: every step
// is exact integer arithmetic and the only rounding is the final >>>12.  The
// per-input multipliers shrink to 24x4, the 16-bit scale multiply moves out of
// the per-input path (one product per group/channel), and the per-channel
// adder trees carry 29-bit terms instead of 45-bit, so ABC maps a far smaller
// network.  Ports/parameters are unchanged.
//
// Structural change (Round-95, gate count / area): the closure log measured
// this leaf's pre-ABC network at 3,087,701 gate nodes for the IN=64/OUT=8/
// NCHAN=8 paramod -- ~6,000 nodes per (input,channel) tap, which is NOT the
// cost of a 24x4 product.  It is a context-widening artifact: the tap product
// is an operand of the S_xq accumulation, so Verilog sizes both of its
// operands to that assignment's width (37 bits) and Yosys techmaps a full
// 37x37 array -- 1,369 AND nodes plus a 36-row adder array per tap, 32 of
// whose rows are constant zero and only disappear later, inside ABC.  Three
// changes remove it:
//   (a) the tap product is written as its four exact partial products
//       (q4[b] ? x<<b : 0), so a tap costs 4 mask rows and 3 adders and no
//       operand is ever extended to the accumulation width
//       -- superseded by Round-96, which removes the per-tap product entirely;
//   (b) every intermediate is sized from the proven magnitude of the value it
//       carries rather than the previous worst case, which narrows every adder
//       and the two surviving per-channel multiplies;
//   (c) each accumulation is a LANES-way balanced reduction with an explicit
//       pairwise tree instead of one GROUP-deep carry chain.
// All three are exact integer identities (see the width derivation below), so
// the result is bit-identical; the only rounding remains the final >>>12.
// Ports, parameters, clock/reset and functional invariants are unchanged.
//
// Structural change (Round-96, ABC front-end width): the Round-95 body still
// carried every intermediate of the weighted sum at TERM_W =
// DATA_W+4+clog2(TG).  Each tap spent four TERM_W-wide adds (three to assemble
// its product, one to accumulate it) plus four TERM_W-wide mask rows, because a
// product's magnitude bound includes the 4 + clog2(TG) bits that are really
// headroom for the SUM of many taps, not for one product.  The sum is now
// reduced bit-plane-wise, so those bits are only paid once per group:
//   x[i]*q4[i] = SUM_k 2**k * (x[i] * q4[i][k])
//   S_xq       = SUM_k 2**k * P_k,   P_k = SUM_{i : q4[i][k]} x[i]
// Each plane gets its own LANES-way reduction, sized to one plane's magnitude
// (SX_W = DATA_W+clog2(TG)); the four planes are merged once per group with
// explicit sign extensions and shifts.  A tap therefore forms a plane term
// instead of a product carried at the accumulation width, and no tap product is
// ever materialised at all.  The identity is exact, so the only rounding is
// still the final >>>12; ports, parameters, clock/reset and functional
// invariants are unchanged.
//
// Structural change (Round-97, scalar tail): after Round-96 the per-group tap
// network held no generic product, but the two per-(group,channel) scalar
// terms were still written with `*`, so the assignment context sized each to
// the accumulator: zero_point*S_x[g] became a 5 x TERM_W array and
// scale_q12*inner an ACC_W x ACC_W one, even though the scale operand is only
// 16 bits and `inner` only TERM_W wide -- most of that array was sign-replica
// rows.  Both are now explicit mask/shift/add trees at the magnitude they
// really carry:
//   zero_point * S_x[g] -- zero_point in [0,15] gives four TERM_W-wide mask
//     rows (S_x shifted by 0..3) combined by a three-add tree;
//     |zp*S_x| <= 15*2**(SX_W-1) < 2**(TERM_W-1), so no row overflows.
//   scale_q12 * inner   -- one mask row per scale bit at PROD_W = TERM_W+15
//     bits, reduced by a four-level pairwise tree; bit 15 carries weight
//     -2**15, so the sign row is the only subtraction.
// Every row is an exact integer multiple of S_x[g] or inner, so both products
// are unchanged and the only rounding is still the final >>>12; ports,
// parameters, clock/reset and functional invariants are unchanged.
module q4_dequant_matvec_ch #(
  parameter int IN        = 64,
  parameter int OUT       = 64,
  parameter int GROUP     = 64,
  parameter int DATA_W    = 24,
  parameter int FRAC_W    = 14,
  parameter int CHAN_BASE = 0,
  parameter int NCHAN     = 1,
  localparam int GROUPS   = (IN + GROUP - 1) / GROUP
) (
  input  logic signed [IN*DATA_W-1:0]      x_bus,
  input  logic        [(IN/2)*OUT*8-1:0]   q_bus,
  input  logic signed [GROUPS*OUT*16-1:0]  scale_bus, // Q3.12
  input  logic        [GROUPS*OUT*8-1:0]   zero_bus,
  output logic signed [NCHAN*DATA_W-1:0]   y
);
  // Exact intermediate widths.  Each is derived from the magnitude of the
  // value it must carry, not from the widest thing that could reach it:
  //   |x[i]| <= 2**(DATA_W-1);  q4, zero_point in [0,15]  => |x*q4| < 2**(DATA_W+3)
  //   group term count <= TG = min(GROUP, IN)
  //   |S_x|   <= TG*2**(DATA_W-1)                        -> DATA_W + clog2(TG)
  //     -- a single bit-plane sum P_k = SUM_{i:q4[i][k]} x[i] carries the same
  //        bound, so SX_W sizes the plane accumulators and every intermediate
  //        that precedes the once-per-group plane merge.
  //   |inner| = |SUM x*(q4-z)| <= 15*SUM|x| <= TG*15*2**(DATA_W-1)
  //           < TG*2**(DATA_W+3)                         -> DATA_W + 4 + clog2(TG)
  //     -- S_xq and z*S_x carry that same bound, and the difference of two
  //        such sums stays inside 15*SUM|x|, so one width covers all three.
  //   |acc|   = |SUM_g scale*inner| <= 15*2**15*IN*2**(DATA_W-1)
  //           < IN*2**(DATA_W+18)                        -> DATA_W + 19 + clog2(IN)
  // The accumulator spans every group, so its bound scales with IN while the
  // per-group terms scale with TG.  All arithmetic is exact at these widths,
  // so the narrowing only removes adders; no result bit can change.
  localparam int TG     = (GROUP < IN) ? GROUP : IN;
  localparam int SX_W   = DATA_W + $clog2(TG);
  localparam int TERM_W = DATA_W + 4 + $clog2(TG);
  localparam int ACC_W  = DATA_W + 19 + $clog2(IN);
  // The Q3.12 scale times one group term: |scale*inner| < 2**15 * 2**(TERM_W-1)
  // = 2**(TERM_W+14), so TERM_W+15 signed bits hold the product exactly and the
  // product never has to be formed -- or added -- at the accumulator width.
  // PROD_W <= ACC_W because TG <= IN and both widths then grow alike.
  localparam int PROD_W = TERM_W + 15;
  // Reduction fan-in: LANES independent partial-sum chains plus an explicit
  // pairwise tree, so a group's depth is TG/LANES + 2 instead of TG.  Integer
  // addition is associative and every partial sum is inside the bounds above,
  // so the reordered sum is bit-identical; a lane whose first term lands in a
  // constant zero costs no gate after constant folding.  The pairwise combine
  // below is written out for exactly this fan-in.
  localparam int LANES  = 4;
  // The weighted sum is reduced one bit plane at a time: q4 is a 4-bit unsigned
  // weight, so its four bit planes are accumulated separately and merged once.
  localparam int PLANES = 4;

  integer i, c, g, b, k;
  logic [7:0] packed_byte;
  logic bit_k;
  logic [3:0] zero_point;
  logic signed [15:0] scale_q12;
  logic signed [DATA_W-1:0] x_value;
  // x_value sign-extended to a plane term's width, and one tap's contribution
  // to one bit plane: that value gated by the plane's weight bit.
  logic signed [SX_W-1:0] x_sext;
  logic signed [SX_W-1:0] lane_term;
  // S_x[g]: sum of the group's inputs, shared by every channel of this leaf.
  logic signed [SX_W-1:0] S_x [GROUPS];
  // S_xq: sum of the group's x*q4 taps.
  logic signed [TERM_W-1:0] S_xq;
  // zSx: zero point times S_x[g];  inner: their difference, i.e. SUM x*(q4-z).
  logic signed [TERM_W-1:0] zSx;
  logic signed [TERM_W-1:0] inner;
  // Scalar-tail trees: S_x[g] and inner at the width their tree needs, the
  // zero_point mask rows, and the signed scale rows/partial products.
  logic signed [TERM_W-1:0] sx_ext;
  logic signed [PROD_W-1:0] inner_ext;
  logic signed [PROD_W-1:0] scale_row [16];
  logic signed [PROD_W-1:0] prod_l [8];
  logic signed [PROD_W-1:0] prod_m [4];
  logic signed [PROD_W-1:0] prod_n [2];
  logic signed [PROD_W-1:0] scale_prod;
  // Lane partial sums and the explicit pairwise combine of each reduction.
  logic signed [SX_W-1:0]   sx_lane  [LANES];
  logic signed [ACC_W-1:0]  acc_lane [LANES];
  logic signed [ACC_W-1:0]  accumulator;
  // Bit-plane weighted-input reduction: one LANES-way reduction per weight bit
  // plane, its pairwise combine, and the four plane sums sign-extended to the
  // merged accumulation width.
  logic signed [SX_W-1:0]   plane_lane [PLANES*LANES];
  logic signed [SX_W-1:0]   plane_sum  [PLANES];
  logic signed [TERM_W-1:0] sx_p0, sx_p1, sx_p2, sx_p3;

  always_comb begin
    // Per-group shared input sums (independent of output channel).
    for (g = 0; g < GROUPS; g = g + 1) begin
      for (b = 0; b < LANES; b = b + 1) sx_lane[b] = '0;
      for (i = g*GROUP; i < (g+1)*GROUP; i = i + LANES) begin
        for (b = 0; b < LANES; b = b + 1) begin
          if (i + b < (g+1)*GROUP && i + b < IN) begin
            x_value = $signed(x_bus[(i + b)*DATA_W +: DATA_W]);
            sx_lane[b] = sx_lane[b] + x_value;
          end
        end
      end
      S_x[g] = (sx_lane[0] + sx_lane[1]) + (sx_lane[2] + sx_lane[3]);
    end

    y = '0;
    for (c = 0; c < NCHAN; c = c + 1) begin
      for (b = 0; b < LANES; b = b + 1) acc_lane[b] = '0;
      for (g = 0; g < GROUPS; g = g + 1) begin
        // Weighted-input sum for this output channel within group g, reduced
        // bit plane by bit plane:
        //   S_xq = SUM_i x[i]*q4[i] = SUM_k 2**k * P_k
        //   P_k  = SUM_{i : q4[i][k]} x[i]
        // Each plane is accumulated on its own LANES-way reduction, so a tap
        // forms a plane term of SX_W bits and no operand is ever extended to
        // the merged accumulation width; the four planes are merged once.
        for (k = 0; k < PLANES*LANES; k = k + 1) plane_lane[k] = '0;
        for (i = g*GROUP; i < (g+1)*GROUP; i = i + LANES) begin
          for (b = 0; b < LANES; b = b + 1) begin
            if (i + b < (g+1)*GROUP && i + b < IN) begin
              packed_byte = q_bus[(((i + b) / 2) * OUT + (CHAN_BASE + c)) * 8 +: 8];
              x_value = $signed(x_bus[(i + b)*DATA_W +: DATA_W]);
              // x * q4 as the sum of its four bit planes: bit k of the tap's
              // nibble gates x itself.  The sign extension is built explicitly
              // at the plane width, so each term is an SX_W-bit pattern of the
              // right value whatever signedness the toolchain infers for `&`,
              // and no mask or add is ever wider than one plane's magnitude.
              x_sext = {{(SX_W - DATA_W){x_value[DATA_W-1]}}, x_value};
              for (k = 0; k < PLANES; k = k + 1) begin
                // The tap's nibble is the low half of its byte for even inputs
                // and the high half for odd inputs.
                bit_k = packed_byte[((i + b) % 2) * 4 + k];
                lane_term = x_sext & {SX_W{bit_k}};
                plane_lane[k*LANES + b] = plane_lane[k*LANES + b] + lane_term;
              end
            end
          end
        end
        for (k = 0; k < PLANES; k = k + 1)
          plane_sum[k] = (plane_lane[k*LANES + 0] + plane_lane[k*LANES + 1])
                       + (plane_lane[k*LANES + 2] + plane_lane[k*LANES + 3]);
        // One shift-merge per group: P_k carries the weight 2**k.  Each plane is
        // sign-extended to TERM_W first, so the shift only drops replicated sign
        // bits and every add operand is already TERM_W wide.
        sx_p0 = {{(TERM_W - SX_W){plane_sum[0][SX_W-1]}}, plane_sum[0]};
        sx_p1 = {{(TERM_W - SX_W){plane_sum[1][SX_W-1]}}, plane_sum[1]};
        sx_p2 = {{(TERM_W - SX_W){plane_sum[2][SX_W-1]}}, plane_sum[2]};
        sx_p3 = {{(TERM_W - SX_W){plane_sum[3][SX_W-1]}}, plane_sum[3]};
        S_xq  = ((sx_p3 << 3) + (sx_p2 << 2)) + ((sx_p1 << 1) + sx_p0);
        zero_point = zero_bus[(g * OUT + (CHAN_BASE + c)) * 8 +: 4];
        scale_q12 = $signed(scale_bus[(g * OUT + (CHAN_BASE + c)) * 16 +: 16]);
        // zero_point * S_x[g]: zero_point is 4 bits, so the product is four
        // mask rows of S_x[g] shifted by 0..3, combined pairwise.  S_x[g] is
        // sign-extended once to TERM_W = SX_W+4, which holds every row
        // (|S_x << 3| <= 2**(SX_W+2)) and the total (<= 15*2**(SX_W-1)).
        sx_ext = {{(TERM_W - SX_W){S_x[g][SX_W-1]}}, S_x[g]};
        zSx = ((zero_point[0] ? sx_ext : '0) + (zero_point[1] ? (sx_ext << 1) : '0))
            + ((zero_point[2] ? (sx_ext << 2) : '0) + (zero_point[3] ? (sx_ext << 3) : '0));
        inner = S_xq - zSx;
        // scale_q12 * inner: one mask row per scale bit at PROD_W = TERM_W+15,
        // the width that holds the exact signed product, reduced by a
        // four-level pairwise tree.  Bit 15 has weight -2**15, so its row is
        // the only subtraction; every other row is inner shifted by k bits.
        inner_ext = {{(PROD_W - TERM_W){inner[TERM_W-1]}}, inner};
        for (k = 0; k < 15; k = k + 1)
          scale_row[k] = scale_q12[k] ? (inner_ext << k) : '0;
        scale_row[15] = scale_q12[15] ? -(inner_ext << 15) : '0;
        for (k = 0; k < 8; k = k + 1) prod_l[k] = scale_row[2*k] + scale_row[2*k + 1];
        for (k = 0; k < 4; k = k + 1) prod_m[k] = prod_l[2*k] + prod_l[2*k + 1];
        for (k = 0; k < 2; k = k + 1) prod_n[k] = prod_m[2*k] + prod_m[2*k + 1];
        scale_prod = prod_n[0] + prod_n[1];
        acc_lane[g % LANES] = acc_lane[g % LANES] + scale_prod;
      end
      accumulator = (acc_lane[0] + acc_lane[1]) + (acc_lane[2] + acc_lane[3]);
      // x is Q*.14 and weight is Q*.12; restore a Q*.14 output.
      y[c*DATA_W +: DATA_W] = accumulator >>> 12;
    end
  end
endmodule

// Fused int4(q/s/z) dequantization and one vector-by-matrix multiply.
// q is packed along IN: low nibble is input 2*r, high nibble input 2*r+1.
// The OUT columns are a generate of CHAN_BLK-column leaves, so no single
// module holds the full IN*OUT body during Yosys genrtlil; the wrapper is
// just ceil(OUT/CHAN_BLK) cheap instantiations plus a packed output gather.
module q4_dequant_matvec #(
  parameter int IN       = 64,
  parameter int OUT      = 64,
  parameter int GROUP    = 64,
  parameter int DATA_W   = 24,
  parameter int FRAC_W   = 14,
  localparam int GROUPS  = (IN + GROUP - 1) / GROUP,
  localparam int CHAN_BLK = 8
) (
  input  logic signed [IN*DATA_W-1:0]          x_bus,
  input  logic        [(IN/2)*OUT*8-1:0]       q_bus,
  input  logic signed [GROUPS*OUT*16-1:0]      scale_bus, // Q3.12
  // Standard image ABI stores each zero point as one uint8; only its low
  // nibble is meaningful.  Do not compact this stream in the compute path.
  input  logic        [GROUPS*OUT*8-1:0]       zero_bus,
  output logic signed [OUT*DATA_W-1:0]         y_bus
);
  logic signed [DATA_W-1:0] y_chan [OUT];

  generate
    for (genvar j = 0; j < OUT; j = j + CHAN_BLK) begin : g_blk
      localparam int NCHAN = (OUT - j < CHAN_BLK) ? (OUT - j) : CHAN_BLK;
      logic signed [NCHAN*DATA_W-1:0] y_leaf;
      q4_dequant_matvec_ch #(
        .IN(IN), .OUT(OUT), .GROUP(GROUP), .DATA_W(DATA_W), .FRAC_W(FRAC_W),
        .CHAN_BASE(j), .NCHAN(NCHAN)
      ) u_ch (
        .x_bus, .q_bus, .scale_bus, .zero_bus, .y(y_leaf)
      );
      for (genvar c = 0; c < NCHAN; c = c + 1) begin : g_y
        assign y_chan[j + c] = y_leaf[c*DATA_W +: DATA_W];
      end
    end
  endgenerate

  always_comb begin
    y_bus = '0;
    for (int c = 0; c < OUT; c = c + 1)
      y_bus[c*DATA_W +: DATA_W] = y_chan[c];
  end
endmodule
