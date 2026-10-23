"""Radiometric surface anomaly engine for 160x120 centi-Kelvin frames.
Derived from the project's existing radiometric capture script.
Thresholds are configurable screening heuristics, not regulatory limits.
"""
import os
import sys
import time
import math
import zlib
import struct
import json
import argparse
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

# ==============================================================================
# CONFIGURATION CONSTANTS & DEFAULTS
# ==============================================================================

# Sensor dimensions (FLIR Lepton 3.5)
LEPTON_WIDTH = 160
LEPTON_HEIGHT = 120
LEPTON_TOTAL_PIXELS = LEPTON_WIDTH * LEPTON_HEIGHT
RAW_FRAME_BYTES = LEPTON_TOTAL_PIXELS * 2  # 38,400 bytes

# Configurable starting point; not a regulatory limit (site-calibrated screening)
LANDFILL_SCREENING_FLOOR_CELSIUS = 55.0
LANDFILL_MAD_MULTIPLIER = 3.5
NORMAL_CONSISTENCY_FACTOR = 1.4826  # Factor converting MAD to normal std dev

# Human Body Bench Test Preset (Desk testing on MacBook)
DEFAULT_BENCH_BODY_THRESHOLD_CELSIUS = 36.0

# Noise filtering defaults
DEFAULT_MIN_CLUSTER_PIXELS = 4     # Rejects single-pixel noise
DEFAULT_MAX_CLUSTER_PIXELS = 15000 # Rejects sensor saturation/flare

# Temporal debounce & cooldown defaults
DEFAULT_DEBOUNCE_FRAMES = 3        # Must persist for 3 consecutive frames
DEFAULT_COOLDOWN_SECONDS = 5.0     # 5 seconds minimum between disk captures
DEFAULT_HYSTERESIS_CELSIUS = 2.0   # De-activation offset below threshold


# ==============================================================================
# RADIOMETRIC MATH & SPATIAL EXTRACTION (Standard Library)
# ==============================================================================

def centikelvin_to_celsius(raw_dn: int) -> float:
    """Converts a single 16-bit centi-Kelvin Digital Number (DN) to Celsius."""
    return (raw_dn / 100.0) - 273.15

def celsius_to_centikelvin(celsius: float) -> int:
    """Converts a Celsius value to 16-bit centi-Kelvin Digital Number (DN)."""
    val = int(round((celsius + 273.15) * 100.0))
    return max(0, min(65535, val))

def calculate_median(values: List[float]) -> float:
    """Computes median of a float list."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0

def calculate_mad(values: List[float], median_val: float) -> float:
    """Computes Median Absolute Deviation (MAD): median(|x_i - median(x)|)."""
    if not values:
        return 0.0
    deviations = [abs(x - median_val) for x in values]
    return calculate_median(deviations)


class DisjointSetUnion:
    """Disjoint-Set Union (DSU) data structure with path compression and union by rank."""
    def __init__(self):
        self.parent = {}
        self.rank = {}

    def make_set(self, x: int):
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # Path compression
        curr = x
        while curr != root:
            nxt = self.parent[curr]
            self.parent[curr] = root
            curr = nxt
        return root

    def union(self, x: int, y: int):
        root_x = self.find(x)
        root_y = self.find(y)
        if root_x == root_y:
            return
        if self.rank[root_x] < self.rank[root_y]:
            self.parent[root_x] = root_y
        elif self.rank[root_x] > self.rank[root_y]:
            self.parent[root_y] = root_x
        else:
            self.parent[root_y] = root_x
            self.rank[root_x] += 1


class Hotspot:
    """Represents a localized thermal hotspot cluster with photogrammetric ground anchors."""
    def __init__(
        self,
        hotspot_id: int,
        peak_celsius: float,
        mean_celsius: float,
        pixel_count: int,
        peak_pixel: Tuple[int, int],
        subpixel_centroid: Tuple[float, float],
        bounding_box: Tuple[int, int, int, int],  # x, y, width, height
        thermal_energy_score: float,
        ground_contact_anchor: Dict[str, Any]
    ):
        self.id = hotspot_id
        self.peak_celsius = peak_celsius
        self.mean_celsius = mean_celsius
        self.pixel_count = pixel_count
        self.peak_pixel = peak_pixel
        self.subpixel_centroid = subpixel_centroid
        self.bounding_box = bounding_box
        self.thermal_energy_score = thermal_energy_score
        self.ground_contact_anchor = ground_contact_anchor

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "peak_temperature_celsius": round(self.peak_celsius, 2),
            "mean_temperature_celsius": round(self.mean_celsius, 2),
            "pixel_count": self.pixel_count,
            "peak_pixel": {"x": self.peak_pixel[0], "y": self.peak_pixel[1]},
            "subpixel_centroid": {
                "u": round(self.subpixel_centroid[0], 2),
                "v": round(self.subpixel_centroid[1], 2)
            },
            "bounding_box_pixels": {
                "x": self.bounding_box[0],
                "y": self.bounding_box[1],
                "width": self.bounding_box[2],
                "height": self.bounding_box[3]
            },
            "ground_contact_anchor": self.ground_contact_anchor,
            "excess_energy_score": round(self.thermal_energy_score, 2)
        }


def extract_hotspots_8ccl(
    celsius_frame: List[float],
    width: int,
    height: int,
    threshold_celsius: float,
    background_celsius: float,
    min_area_pixels: int = DEFAULT_MIN_CLUSTER_PIXELS,
    max_area_pixels: int = DEFAULT_MAX_CLUSTER_PIXELS
) -> List[Hotspot]:
    """
    Two-pass 8-connected Connected Component Labeling (CCL) with
    Disjoint-Set Union (DSU) and Radiometric Sub-Pixel Intensity-Weighted
    centroid calculation.
    """
    total_pixels = width * height
    labels = [0] * total_pixels
    dsu = DisjointSetUnion()
    next_label = 1

    # PASS 1: Raster scan and label assignment with DSU equivalence merging
    for y in range(height):
        row_offset = y * width
        for x in range(width):
            idx = row_offset + x
            temp = celsius_frame[idx]
            if math.isnan(temp) or temp < threshold_celsius:
                continue

            # Check 4 prior 8-connected neighbors:
            # (-1, -1) [top-left], (0, -1) [top], (1, -1) [top-right], (-1, 0) [left]
            prior_neighbor_labels = []

            # top-left
            if y > 0 and x > 0:
                l = labels[(y - 1) * width + (x - 1)]
                if l > 0: prior_neighbor_labels.append(l)
            # top
            if y > 0:
                l = labels[(y - 1) * width + x]
                if l > 0: prior_neighbor_labels.append(l)
            # top-right
            if y > 0 and x + 1 < width:
                l = labels[(y - 1) * width + (x + 1)]
                if l > 0: prior_neighbor_labels.append(l)
            # left
            if x > 0:
                l = labels[y * width + (x - 1)]
                if l > 0: prior_neighbor_labels.append(l)

            if not prior_neighbor_labels:
                # New component seed
                labels[idx] = next_label
                dsu.make_set(next_label)
                next_label += 1
            else:
                # Assign minimum label and union all adjacent labels
                min_l = min(prior_neighbor_labels)
                labels[idx] = min_l
                for l in prior_neighbor_labels:
                    if l != min_l:
                        dsu.union(min_l, l)

    if next_label == 1:
        return []

    # PASS 2: Canonicalize labels and group pixels by component
    components_map: Dict[int, List[int]] = {}
    for idx in range(total_pixels):
        lbl = labels[idx]
        if lbl > 0:
            canon = dsu.find(lbl)
            if canon not in components_map:
                components_map[canon] = []
            components_map[canon].append(idx)

    # Process and measure each detected component
    components = []
    for canon_id, pixel_indices in components_map.items():
        pixel_count = len(pixel_indices)
        if not (min_area_pixels <= pixel_count <= max_area_pixels):
            continue

        min_x = width
        max_x = 0
        min_y = height
        max_y = 0
        peak_temp = -999.0
        peak_x = 0
        peak_y = 0
        temp_sum = 0.0

        sum_weights = 0.0
        sum_weighted_x = 0.0
        sum_weighted_y = 0.0

        for p_idx in pixel_indices:
            px = p_idx % width
            py = p_idx // width
            t = celsius_frame[p_idx]

            temp_sum += t
            if t > peak_temp:
                peak_temp = t
                peak_x = px
                peak_y = py

            if px < min_x: min_x = px
            if px > max_x: max_x = px
            if py < min_y: min_y = py
            if py > max_y: max_y = py

            # Excess radiance weight above background
            weight = max(0.01, t - background_celsius)
            sum_weights += weight
            sum_weighted_x += weight * px
            sum_weighted_y += weight * py

        # Subpixel centroid calculation
        sub_u = (sum_weighted_x / sum_weights) if sum_weights > 0 else float(peak_x)
        sub_v = (sum_weighted_y / sum_weights) if sum_weights > 0 else float(peak_y)

        bbox_w = max_x - min_x + 1
        bbox_h = max_y - min_y + 1
        mean_temp = temp_sum / pixel_count

        # Image-space lower-edge anchor (not a measured ground coordinate)
        ground_anchors = {
            "planar_2d_centroid": {
                "u": round(sub_u, 2),
                "v": round(sub_v, 2)
            },
            "volumetric_3d_bottom_center": {
                "u": round(min_x + bbox_w / 2.0, 2),
                "v": round(float(max_y + 1), 2)
            }
        }

        components.append({
            "peak_celsius": peak_temp,
            "mean_celsius": mean_temp,
            "pixel_count": pixel_count,
            "peak_pixel": (peak_x, peak_y),
            "subpixel_centroid": (sub_u, sub_v),
            "bounding_box": (min_x, min_y, bbox_w, bbox_h),
            "thermal_energy_score": sum_weights,
            "ground_contact_anchor": ground_anchors
        })

    # Sort hotspots descending by peak temperature (highest hazard first)
    components.sort(key=lambda c: c["peak_celsius"], reverse=True)

    result_hotspots = []
    for i, c in enumerate(components):
        result_hotspots.append(Hotspot(
            hotspot_id=i,
            peak_celsius=c["peak_celsius"],
            mean_celsius=c["mean_celsius"],
            pixel_count=c["pixel_count"],
            peak_pixel=c["peak_pixel"],
            subpixel_centroid=c["subpixel_centroid"],
            bounding_box=c["bounding_box"],
            thermal_energy_score=c["thermal_energy_score"],
            ground_contact_anchor=c["ground_contact_anchor"]
        ))
    return result_hotspots


# ==============================================================================
# ZERO-DEPENDENCY COLORMAP & PNG EXPORTER
# ==============================================================================

def inferno_colormap(norm_val: float) -> Tuple[int, int, int]:
    """
    Interpolates a normalized [0, 1] temperature to a high-contrast Inferno thermal palette.
    Returns (R, G, B) in range [0, 255].
    """
    val = max(0.0, min(1.0, norm_val))
    # 5 anchor points: Black -> Purple -> Red-Orange -> Yellow -> White
    anchors = [
        (0.00, (0, 0, 4)),
        (0.25, (87, 16, 110)),
        (0.50, (187, 55, 84)),
        (0.75, (249, 142, 9)),
        (1.00, (252, 255, 164))
    ]
    for i in range(len(anchors) - 1):
        t0, c0 = anchors[i]
        t1, c1 = anchors[i + 1]
        if t0 <= val <= t1:
            ratio = (val - t0) / (t1 - t0)
            r = int(c0[0] + ratio * (c1[0] - c0[0]))
            g = int(c0[1] + ratio * (c1[1] - c0[1]))
            b = int(c0[2] + ratio * (c1[2] - c0[2]))
            return (r, g, b)
    return (255, 255, 255)


def generate_png_bytes(width: int, height: int, rgb_bytes: bytes) -> bytes:
    """Generates standard 24-bit PNG file bytes using standard library zlib."""
    def make_chunk(chunk_type: bytes, data: bytes) -> bytes:
        length = struct.pack('>I', len(data))
        crc = struct.pack('>I', zlib.crc32(chunk_type + data) & 0xffffffff)
        return length + chunk_type + data + crc

    png_signature = b'\x89PNG\r\n\x1a\n'
    ihdr_payload = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0) # 8-bit truecolor
    ihdr_chunk = make_chunk(b'IHDR', ihdr_payload)

    raw_scanlines = bytearray()
    row_bytes = width * 3
    for y in range(height):
        raw_scanlines.append(0)  # Filter type 0: None
        start = y * row_bytes
        raw_scanlines.extend(rgb_bytes[start:start + row_bytes])

    idat_chunk = make_chunk(b'IDAT', zlib.compress(bytes(raw_scanlines), level=6))
    iend_chunk = make_chunk(b'IEND', b'')

    return png_signature + ihdr_chunk + idat_chunk + iend_chunk


def render_annotated_thermal_image(
    celsius_frame: List[float],
    width: int,
    height: int,
    hotspots: List[Hotspot],
    t_min: float,
    t_max: float
) -> bytes:
    """
    Renders false-color image and draws bounding boxes + subpixel crosshairs.
    Returns PNG file bytes.
    """
    range_t = max(1.0, t_max - t_min)
    rgb_grid = bytearray(width * height * 3)

    # 1. Base thermal colormap
    for i in range(width * height):
        temp = celsius_frame[i]
        norm = (temp - t_min) / range_t
        r, g, b = inferno_colormap(norm)
        idx = i * 3
        rgb_grid[idx] = r
        rgb_grid[idx + 1] = g
        rgb_grid[idx + 2] = b

    def set_pixel(px: int, py: int, color: Tuple[int, int, int]):
        if 0 <= px < width and 0 <= py < height:
            idx = (py * width + px) * 3
            rgb_grid[idx] = color[0]
            rgb_grid[idx + 1] = color[1]
            rgb_grid[idx + 2] = color[2]

    # 2. Draw annotations for each hotspot
    yellow = (255, 255, 0)
    red = (255, 0, 0)
    green = (0, 255, 0)

    for h in hotspots:
        bx, by, bw, bh = h.bounding_box
        # Bounding box border
        for px in range(bx, bx + bw):
            set_pixel(px, by, yellow)
            set_pixel(px, by + bh - 1, yellow)
        for py in range(by, by + bh):
            set_pixel(bx, py, yellow)
            set_pixel(bx + bw - 1, py, yellow)

        # Subpixel centroid crosshair (+)
        cu = int(round(h.subpixel_centroid[0]))
        cv = int(round(h.subpixel_centroid[1]))
        for offset in range(-3, 4):
            set_pixel(cu + offset, cv, green)
            set_pixel(cu, cv + offset, green)

        # Peak pixel indicator
        px, py = h.peak_pixel
        set_pixel(px, py, red)

    return generate_png_bytes(width, height, bytes(rgb_grid))


# ==============================================================================
# RULE-BASED AUTO-CAPTURE ENGINE & STATE MACHINE
# ==============================================================================

class TriggerMode:
    BENCH_BODY = "bench_human_body"
    LANDFILL_PROD = "landfill_production"


class CaptureState:
    IDLE = "IDLE"
    DEBOUNCING = "DEBOUNCING"
    TRIGGERED = "TRIGGERED"
    COOLDOWN = "COOLDOWN"


class RadiometricAutoCaptureEngine:
    """
    Autonomous rule-based engine that processes radiometric frames and
    manages the debounce, trigger, and cooldown state machine.
    """
    def __init__(
        self,
        mode: str = TriggerMode.BENCH_BODY,
        custom_threshold: Optional[float] = None,
        min_cluster_pixels: int = DEFAULT_MIN_CLUSTER_PIXELS,
        debounce_frames: int = DEFAULT_DEBOUNCE_FRAMES,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        output_dir: str = "./captures",
        delta_celsius: float = 10.0,
        mad_multiplier: float = 3.5,
        hysteresis_celsius: float = 2.0,
        persistence_seconds: float = 0.0,
        source: str = "unspecified",
        max_cluster_pixels: int = 15000
    ):
        numeric = [delta_celsius, mad_multiplier, hysteresis_celsius, persistence_seconds, cooldown_seconds]
        if not all(math.isfinite(v) and v >= 0 for v in numeric):
            raise ValueError("Rule values must be finite and non-negative")
        if custom_threshold is not None and (not math.isfinite(custom_threshold) or not -40 <= custom_threshold <= 300):
            raise ValueError("Threshold must be between -40 and 300°C")
        if not 1 <= min_cluster_pixels <= 15000 or not 1 <= debounce_frames <= 1000:
            raise ValueError("Invalid area or debounce count")
        if mode not in (TriggerMode.BENCH_BODY, TriggerMode.LANDFILL_PROD):
            raise ValueError("Unknown trigger mode")
        if type(min_cluster_pixels) is not int or type(debounce_frames) is not int or type(max_cluster_pixels) is not int:
            raise ValueError("Area and frame counts must be integers")
        if not min_cluster_pixels <= max_cluster_pixels <= LEPTON_TOTAL_PIXELS:
            raise ValueError("Invalid maximum area")
        self.max_cluster_pixels = max_cluster_pixels
        self.last_frame_clock = None
        self.trigger_threshold = None
        self.delta_celsius = delta_celsius
        self.mad_multiplier = mad_multiplier
        self.hysteresis_celsius = hysteresis_celsius
        self.persistence_seconds = persistence_seconds
        self.source = source
        self.first_seen = None
        self.previous_boxes = []
        self.mode = mode
        self.custom_threshold = custom_threshold
        self.min_cluster_pixels = min_cluster_pixels
        self.debounce_frames = debounce_frames
        self.cooldown_seconds = cooldown_seconds
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # State machine tracking
        self.state = CaptureState.IDLE
        self.consecutive_exceed_count = 0
        self.last_capture_time = 0.0
        self.total_captures = 0

    def reset_persistence(self):
        self.consecutive_exceed_count = 0
        self.first_seen = None
        self.previous_boxes = []
        if self.state != CaptureState.COOLDOWN:
            self.state = CaptureState.IDLE

    def compute_dynamic_threshold(self, celsius_frame: List[float]) -> Tuple[float, float, float]:
        """
        Calculates the active temperature threshold based on selected mode.
        Returns: (active_threshold, background_median, mad)
        """
        bg_median = calculate_median(celsius_frame)
        mad = calculate_mad(celsius_frame, bg_median)

        if self.custom_threshold is not None and self.mode != TriggerMode.LANDFILL_PROD:
            # Explicit manual override
            active_thresh = self.custom_threshold
        elif self.mode == TriggerMode.BENCH_BODY:
            # ------------------------------------------------------------------
            # BENCH TESTING PRESET (For MacBook desk testing without live fire)
            # Default: 36.0°C starting point; observed skin temperature varies.
            # ------------------------------------------------------------------
            active_thresh = DEFAULT_BENCH_BODY_THRESHOLD_CELSIUS
        elif self.mode == TriggerMode.LANDFILL_PROD:
            # ------------------------------------------------------------------
            # Site-calibrated landfill screening heuristic
            # T_thresh = max(absolute floor, background + delta, background + k * 1.4826 * MAD)
            # ------------------------------------------------------------------
            statistical_thresh = bg_median + (self.mad_multiplier * NORMAL_CONSISTENCY_FACTOR * mad)
            active_thresh = max(self.custom_threshold if self.custom_threshold is not None else LANDFILL_SCREENING_FLOOR_CELSIUS, bg_median + self.delta_celsius, statistical_thresh)
        else:
            active_thresh = DEFAULT_BENCH_BODY_THRESHOLD_CELSIUS

        return active_thresh, bg_median, mad

    def process_frame(
        self,
        raw_uint16: List[int],
        timestamp: Optional[float] = None,
        clock: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Processes one 16-bit radiometric frame:
          1. Converts DN to Celsius.
          2. Computes dynamic threshold.
          3. Extracts 8-connected hotspot clusters.
          4. Evaluates debounce & cooldown state machine.
          5. If triggered, saves .bin, .png, and .json.
        """
        wall_time = time.time() if timestamp is None else timestamp
        now = time.monotonic() if clock is None else clock
        if self.last_frame_clock is not None and (now - self.last_frame_clock > 2 or now < self.last_frame_clock):
            self.reset_persistence()
        self.last_frame_clock = now
        if len(raw_uint16) != LEPTON_TOTAL_PIXELS or any(type(v) is not int or not 1 <= v < 65535 for v in raw_uint16):
            self.consecutive_exceed_count = 0
            self.first_seen = None
            self.previous_boxes = []
            if self.state != CaptureState.COOLDOWN:
                self.state = CaptureState.IDLE
            raise ValueError("Expected 19,200 valid Y16 centi-Kelvin pixels; invalid/saturated frame rejected")
        celsius_frame = [centikelvin_to_celsius(val) for val in raw_uint16]

        t_min = min(celsius_frame)
        t_max = max(celsius_frame)
        threshold_celsius, bg_median, mad = self.compute_dynamic_threshold(celsius_frame)

        # Extract discrete hotspot clusters
        hotspots = extract_hotspots_8ccl(
            celsius_frame=celsius_frame,
            width=LEPTON_WIDTH,
            height=LEPTON_HEIGHT,
            threshold_celsius=threshold_celsius,
            background_celsius=bg_median,
            min_area_pixels=self.min_cluster_pixels,
            max_area_pixels=self.max_cluster_pixels
        )

        has_valid_hotspot = len(hotspots) > 0
        peak_hazard_temp = hotspots[0].peak_celsius if has_valid_hotspot else t_max

        # Evaluate State Machine
        action_taken = None
        capture_path = None

        if self.state == CaptureState.COOLDOWN:
            # Check if cooldown has elapsed
            if (now - self.last_capture_time) >= self.cooldown_seconds:
                # Require hysteresis clearance before returning to IDLE
                hysteresis_floor = self.trigger_threshold - self.hysteresis_celsius
                if peak_hazard_temp < hysteresis_floor:
                    self.state = CaptureState.IDLE
                    self.reset_persistence()
            # Else remain in COOLDOWN

        elif self.state in (CaptureState.IDLE, CaptureState.DEBOUNCING):
            if has_valid_hotspot:
                boxes = [h.bounding_box for h in hotspots]
                def overlaps(a, b):
                    return a[0] < b[0]+b[2] and b[0] < a[0]+a[2] and a[1] < b[1]+b[3] and b[1] < a[1]+a[3]
                matched = [b for b in boxes if any(overlaps(b, old) for old in self.previous_boxes)]
                if not matched:
                    self.consecutive_exceed_count = 0
                    self.first_seen = now
                self.previous_boxes = matched or boxes
                self.consecutive_exceed_count += 1
                if self.consecutive_exceed_count >= self.debounce_frames and now - self.first_seen >= self.persistence_seconds:
                    # TRIGGER CONDITION MET!
                    action_taken = "AUTO_CAPTURED"
                    capture_path = self._save_capture_bundle(
                        raw_uint16=raw_uint16,
                        celsius_frame=celsius_frame,
                        hotspots=hotspots,
                        threshold_celsius=threshold_celsius,
                        bg_median=bg_median,
                        mad=mad,
                        t_min=t_min,
                        t_max=t_max,
                        timestamp=wall_time
                    )
                    self.trigger_threshold = threshold_celsius
                    self.last_capture_time = now
                    self.total_captures += 1
                    self.state = CaptureState.COOLDOWN
                    self.consecutive_exceed_count = 0
                else:
                    self.state = CaptureState.DEBOUNCING
            else:
                self.consecutive_exceed_count = 0
                self.state = CaptureState.IDLE
                self.first_seen = None
                self.previous_boxes = []

        return {
            "state": self.state,
            "threshold_celsius": threshold_celsius,
            "bg_median_celsius": bg_median,
            "mad_celsius": mad,
            "min_celsius": t_min,
            "max_celsius": t_max,
            "hotspot_count": len(hotspots),
            "peak_hazard_celsius": peak_hazard_temp,
            "debounce_counter": f"{self.consecutive_exceed_count}/{self.debounce_frames}",
            "persistence_elapsed_seconds": max(0, now - self.first_seen) if self.first_seen is not None else 0,
            "cooldown_remaining_seconds": max(0, self.cooldown_seconds - (now - self.last_capture_time)),
            "action_taken": action_taken,
            "capture_path": capture_path,
            "hotspots": hotspots
        }

    def _save_capture_bundle(
        self,
        raw_uint16: List[int],
        celsius_frame: List[float],
        hotspots: List[Hotspot],
        threshold_celsius: float,
        bg_median: float,
        mad: float,
        t_min: float,
        t_max: float,
        timestamp: float,
        trigger_kind: str = "rule",
        request_id: Optional[str] = None
    ) -> str:
        """
        Persists atomic bundle: .bin (raw radiometric), .png (annotated visual), .json (metadata).
        """
        timestr = time.strftime("%Y%m%d_%H%M%S", time.localtime(timestamp))
        millis = int((timestamp % 1.0) * 1000)
        event_name = f"{timestr}_{millis:03d}_event_{self.total_captures + 1:03d}"
        import uuid
        event_name += "_" + uuid.uuid4().hex[:8]
        final_dir = self.output_dir / event_name
        event_dir = self.output_dir / ("." + event_name + ".pending")
        event_dir.mkdir(parents=True, exist_ok=False)

        # 1. Save 16-bit raw radiometric binary (.bin)
        # Directly compatible with iOS FolderThermalDataSource
        bin_path = event_dir / "frame_raw.bin"
        raw_bytes = struct.pack(f'<{len(raw_uint16)}H', *raw_uint16)
        with open(bin_path, "wb") as f:
            f.write(raw_bytes)

        # 2. Save annotated visual thermal image (.png)
        png_path = event_dir / "frame_visual.png"
        png_bytes = render_annotated_thermal_image(
            celsius_frame=celsius_frame,
            width=LEPTON_WIDTH,
            height=LEPTON_HEIGHT,
            hotspots=hotspots,
            t_min=t_min,
            t_max=t_max
        )
        with open(png_path, "wb") as f:
            f.write(png_bytes)

        # 3. Save structured metadata (.json)
        json_path = event_dir / "metadata.json"
        metadata = {
            "schema_version": 2,
            "device_id": getattr(self, "device_id", None),
            "radiometry": {"valid": True, "encoding": "centikelvin", "width": LEPTON_WIDTH, "height": LEPTON_HEIGHT, "dtype": "uint16", "byte_order": "little"},
            "event_id": event_name,
            "timestamp_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(timestamp)) + f".{millis:03d}Z",
            "unix_timestamp": timestamp,
            "trigger_mode": self.mode,
            "trigger_kind": trigger_kind,
            "request_id": request_id,
            "rule_basis": "Surface anomaly screening; site validation required; not fire confirmation",
            "source": self.source,
            "rule_config": {"absolute_celsius": self.custom_threshold if self.custom_threshold is not None else (55.0 if self.mode == TriggerMode.LANDFILL_PROD else 36.0), "delta_celsius": self.delta_celsius,
                "mad_multiplier": self.mad_multiplier, "min_cluster_pixels": self.min_cluster_pixels, "max_cluster_pixels": self.max_cluster_pixels,
                "debounce_frames": self.debounce_frames, "persistence_seconds": self.persistence_seconds,
                "cooldown_seconds": self.cooldown_seconds, "hysteresis_celsius": self.hysteresis_celsius},
            "temperature_threshold_celsius": round(threshold_celsius, 2),
            "background_stats": {
                "median_celsius": round(bg_median, 2),
                "mad_celsius": round(mad, 2),
                "min_celsius": round(t_min, 2),
                "max_celsius": round(t_max, 2)
            },
            "hotspot_count": len(hotspots),
            "hotspots": [h.to_dict() for h in hotspots],
            "camera": {
                "model": "FLIR Lepton 3.5",
                "interface": "PureThermal 3 UVC",
                "resolution": {"width": LEPTON_WIDTH, "height": LEPTON_HEIGHT},
                "format": "Y16 (centi-Kelvin)"
            }
        }
        with open(json_path, "w") as f:
            json.dump(metadata, f, indent=2)

        # Flush each file before publishing the completed directory.
        for path in (bin_path, png_path, json_path):
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
        event_dir.rename(final_dir)
        directory_fd = os.open(self.output_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return str(final_dir)
