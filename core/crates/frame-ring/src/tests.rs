//! 单元测试：测试自己创建共享内存并写好头部（生产中由主进程做），再经写入方、读取方与假采集源读写。
//! 本进程对共享内存的访问全是原子操作，所以同进程的并发读写测试没有数据竞争。

use std::mem::offset_of;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::Duration;

use capture::{CaptureError, CaptureSource, Frame, FrameMeta, Point};
use core_protocol::frame_ring::{
    FrameSlotHeader, LAYOUT_VERSION, SLOT_ALIGN, SLOT_PIXELS_OFFSET, SLOTS_OFFSET,
};
use core_protocol::messages::{FakeTarget, FrameRingAttachParams};

use crate::ring::{header as offsets, write_header};
use crate::shm::{Created, SharedMemory};
use crate::{
    FAKE_DPI, FakeCapture, FrameRingError, FrameRingReader, FrameRingWriter, ReadError,
    ReadOutcome, SlotMeta, WriteError,
};

fn unique_name() -> String {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    let id = format!(
        "bellis-frame-ring-test-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    );
    if cfg!(windows) {
        format!("Local\\{id}")
    } else {
        format!("/{id}")
    }
}

fn ring_params(
    name: String,
    slot_count: u32,
    max_width: u32,
    max_height: u32,
) -> FrameRingAttachParams {
    let slot_stride = (SLOT_PIXELS_OFFSET + u64::from(max_width) * u64::from(max_height) * 4)
        .next_multiple_of(SLOT_ALIGN);
    FrameRingAttachParams {
        name,
        size_bytes: SLOTS_OFFSET + u64::from(slot_count) * slot_stride,
        layout_version: LAYOUT_VERSION,
        slot_count,
        slot_stride,
        max_width,
        max_height,
    }
}

/// 测试创建的帧环：共享内存与写好的头部。
struct TestRing {
    params: FrameRingAttachParams,
    memory: SharedMemory,
    _created: Created,
}

impl TestRing {
    fn new(slot_count: u32, max_width: u32, max_height: u32) -> Self {
        let params = ring_params(unique_name(), slot_count, max_width, max_height);
        let size = usize::try_from(params.size_bytes).unwrap();
        let created = Created::new(&params.name, size).unwrap();
        let memory = SharedMemory::open(&params.name, size).unwrap();
        write_header(&memory, &params);
        Self {
            params,
            memory,
            _created: created,
        }
    }

    fn slot_start(&self, slot: u32) -> usize {
        usize::try_from(SLOTS_OFFSET + u64::from(slot) * self.params.slot_stride).unwrap()
    }
}

/// 帧 `number` 的每个像素都是同一个由帧号算出的值，每行末尾另有 `padding` 字节的 0xEE。
fn pattern_word(number: u64) -> [u8; 4] {
    #[allow(clippy::cast_possible_truncation, reason = "只取低 32 位作图案")]
    (number as u32).wrapping_mul(0x9E37_79B1).to_le_bytes()
}

fn pattern_frame(number: u64, width: u32, height: u32, padding: u32) -> Frame {
    let stride = width * 4 + padding;
    let mut pixels = Vec::new();
    for _ in 0..height {
        for _ in 0..width {
            pixels.extend_from_slice(&pattern_word(number));
        }
        pixels.extend(std::iter::repeat_n(0xEE, padding as usize));
    }
    Frame {
        meta: FrameMeta {
            number,
            captured_at_ns: number * 1000 + 7,
            transform_version: number / 5 + 1,
            width,
            height,
            stride,
            client_origin: Point { x: 3, y: 4 },
            dpi: 144,
        },
        pixels,
    }
}

/// 帧的有效像素，去掉行尾填充。
fn compact(frame: &Frame) -> Vec<u8> {
    (0..frame.meta.height)
        .flat_map(|y| frame.row(y).unwrap().to_vec())
        .collect()
}

fn read(reader: &FrameRingReader) -> (SlotMeta, Vec<u8>) {
    let mut pixels = Vec::new();
    match reader.read_latest(&mut pixels, 1).unwrap() {
        ReadOutcome::Frame(meta) => (meta, pixels),
        other => panic!("expected a frame, got {other:?}"),
    }
}

#[test]
fn shared_memory_copies_at_any_alignment() {
    let ring = TestRing::new(2, 4, 4);
    let source: Vec<u8> = (0u8..=40).collect();
    for offset in 64..80 {
        for len in [0, 1, 7, 8, 9, 15, 16, 17, 33] {
            ring.memory.store(offset, &source[..len]);
            let mut back = vec![0xAA; len];
            ring.memory.load(offset, &mut back);
            assert_eq!(back, &source[..len], "offset {offset}, len {len}");
        }
    }
}

#[test]
fn attach_accepts_a_matching_header() {
    let ring = TestRing::new(3, 8, 6);
    let writer = FrameRingWriter::attach(&ring.params).unwrap();
    assert_eq!(writer.last_frame_number(), 0);
    let reader = FrameRingReader::open(&ring.params).unwrap();
    assert_eq!(reader.latest_frame_number(), 0);
    let mut pixels = Vec::new();
    assert_eq!(reader.read_latest(&mut pixels, 1), Ok(ReadOutcome::Empty));
}

#[test]
fn attach_rejects_parameters_that_break_the_layout_rules() {
    type Change = fn(&mut FrameRingAttachParams);
    let ring = TestRing::new(2, 8, 6);
    let cases: [(&str, Change); 8] = [
        ("empty name", |p| p.name.clear()),
        ("NUL in name", |p| p.name.push('\0')),
        ("layout version", |p| p.layout_version += 1),
        ("one slot", |p| p.slot_count = 1),
        ("zero width", |p| p.max_width = 0),
        ("unaligned stride", |p| p.slot_stride += 8),
        ("stride too small", |p| p.slot_stride -= SLOT_ALIGN),
        ("size too small", |p| p.size_bytes -= 1),
    ];
    for (case, change) in cases {
        let mut params = ring.params.clone();
        change(&mut params);
        let error = FrameRingWriter::attach(&params).unwrap_err();
        assert!(
            matches!(error, FrameRingError::InvalidParams(_)),
            "{case}: {error}"
        );
    }
}

#[test]
fn attach_rejects_a_header_that_differs_from_the_parameters() {
    // 每一项：改头部的哪个字段（偏移、宽度）与改成的值，以及预期报告的字段名。
    let cases: [(&str, usize, bool, u64); 6] = [
        ("magic", offsets::MAGIC, true, 1),
        ("layout_version", offsets::LAYOUT_VERSION, false, 9),
        ("slot_count", offsets::SLOT_COUNT, false, 3),
        ("slot_stride", offsets::SLOT_STRIDE, true, 128),
        ("max_width", offsets::MAX_WIDTH, false, 7),
        ("max_height", offsets::MAX_HEIGHT, false, 5),
    ];
    for (field, offset, wide, value) in cases {
        let ring = TestRing::new(2, 8, 6);
        if wide {
            ring.memory.u64_at(offset).store(value, Ordering::Relaxed);
        } else {
            ring.memory
                .u32_at(offset)
                .store(u32::try_from(value).unwrap(), Ordering::Relaxed);
        }
        match FrameRingWriter::attach(&ring.params).unwrap_err() {
            FrameRingError::Mismatch {
                field: reported,
                header,
                ..
            } => {
                assert_eq!((reported, header), (field, value));
            }
            other => panic!("{field}: {other}"),
        }
    }
}

#[test]
fn attach_fails_when_the_shared_memory_is_missing_or_too_small() {
    let missing = ring_params(unique_name(), 2, 8, 6);
    let error = FrameRingWriter::attach(&missing).unwrap_err();
    assert!(matches!(error, FrameRingError::Open { .. }), "{error}");

    // 共享内存只有 64 KiB，参数却要 2 个 256×256 的槽。
    let params = ring_params(unique_name(), 2, 256, 256);
    let _created = Created::new(&params.name, 64 * 1024).unwrap();
    let error = FrameRingWriter::attach(&params).unwrap_err();
    assert!(matches!(error, FrameRingError::Open { .. }), "{error}");
}

#[test]
fn written_frames_read_back_without_row_padding() {
    let ring = TestRing::new(3, 8, 6);
    let mut writer = FrameRingWriter::attach(&ring.params).unwrap();
    let reader = FrameRingReader::open(&ring.params).unwrap();
    for (number, (width, height, padding)) in [(1, (8, 6, 0)), (2, (5, 3, 12)), (3, (1, 1, 4))] {
        let frame = pattern_frame(number, width, height, padding);
        assert_eq!(writer.write(&frame), Ok(number));
        let (meta, pixels) = read(&reader);
        assert_eq!(
            meta,
            SlotMeta {
                frame_number: number,
                capture_ns: frame.meta.captured_at_ns,
                transform_version: frame.meta.transform_version,
                width,
                height,
                row_stride: width * 4,
            }
        );
        assert_eq!(pixels, compact(&frame));
    }
    assert_eq!(reader.latest_frame_number(), 3);
}

#[test]
fn write_rejects_frames_that_do_not_fit_or_are_malformed() {
    let ring = TestRing::new(2, 8, 6);
    let mut writer = FrameRingWriter::attach(&ring.params).unwrap();
    let too_wide = pattern_frame(1, 9, 6, 0);
    assert_eq!(
        writer.write(&too_wide),
        Err(WriteError::TooLarge {
            width: 9,
            height: 6,
            max_width: 8,
            max_height: 6
        })
    );
    let mut zero = pattern_frame(1, 2, 2, 0);
    zero.meta.number = 0;
    let mut empty = pattern_frame(1, 2, 2, 0);
    empty.meta.height = 0;
    let mut narrow = pattern_frame(1, 2, 2, 0);
    narrow.meta.stride = 7;
    let mut short = pattern_frame(1, 2, 2, 0);
    short.pixels.pop();
    for frame in [zero, empty, narrow, short] {
        assert!(matches!(
            writer.write(&frame),
            Err(WriteError::InvalidFrame(_))
        ));
    }
    let reader = FrameRingReader::open(&ring.params).unwrap();
    assert_eq!(
        reader.latest_frame_number(),
        0,
        "rejected frames leave no trace"
    );
}

#[test]
fn frame_numbers_keep_gaps_resume_after_reattach_and_survive_a_new_source() {
    let ring = TestRing::new(3, 4, 4);
    let mut writer = FrameRingWriter::attach(&ring.params).unwrap();
    let numbers: Vec<u64> = [1, 2, 4]
        .into_iter()
        .map(|source| writer.write(&pattern_frame(source, 2, 2, 0)).unwrap())
        .collect();
    assert_eq!(
        numbers,
        [1, 2, 4],
        "a gap in the source numbers stays a gap"
    );
    drop(writer);

    // 实时核重启：从头部的 write_seq 续写，采集源的帧号从 1 重新数起。
    let mut writer = FrameRingWriter::attach(&ring.params).unwrap();
    assert_eq!(writer.last_frame_number(), 4);
    assert_eq!(writer.write(&pattern_frame(1, 2, 2, 0)), Ok(5));
    assert_eq!(writer.write(&pattern_frame(3, 2, 2, 0)), Ok(7));
    // 同一个写入方换了新的采集源。
    assert_eq!(writer.write(&pattern_frame(1, 2, 2, 0)), Ok(8));
    assert_eq!(writer.write(&pattern_frame(2, 2, 2, 0)), Ok(9));
    let reader = FrameRingReader::open(&ring.params).unwrap();
    assert_eq!(read(&reader).0.frame_number, 9);
}

#[test]
fn writer_skips_an_odd_seq_left_by_a_crash() {
    let ring = TestRing::new(2, 4, 4);
    let seq = ring.memory.u64_at(ring.slot_start(0));
    seq.store(5, Ordering::Relaxed);
    let mut writer = FrameRingWriter::attach(&ring.params).unwrap();
    writer.write(&pattern_frame(1, 2, 2, 0)).unwrap();
    assert_eq!(
        seq.load(Ordering::Relaxed),
        8,
        "odd 5 -> 7 while writing -> 8"
    );
    writer.write(&pattern_frame(2, 2, 2, 0)).unwrap();
    writer.write(&pattern_frame(3, 2, 2, 0)).unwrap();
    assert_eq!(seq.load(Ordering::Relaxed), 10);
    let reader = FrameRingReader::open(&ring.params).unwrap();
    assert_eq!(read(&reader).0.frame_number, 3);
}

#[test]
fn reader_retries_and_gives_up_on_torn_slots() {
    let ring = TestRing::new(2, 4, 4);
    let mut writer = FrameRingWriter::attach(&ring.params).unwrap();
    let reader = FrameRingReader::open(&ring.params).unwrap();
    let mut pixels = Vec::new();
    writer.write(&pattern_frame(1, 2, 2, 0)).unwrap();

    // 写方正在写这一槽。
    let seq = ring.memory.u64_at(ring.slot_start(0));
    seq.fetch_add(1, Ordering::Relaxed);
    assert_eq!(reader.read_latest(&mut pixels, 3), Ok(ReadOutcome::Torn));
    seq.fetch_add(1, Ordering::Relaxed);
    assert!(matches!(
        reader.read_latest(&mut pixels, 1),
        Ok(ReadOutcome::Frame(_))
    ));

    // write_seq 指向的帧号与槽里的不同：写方已经超过这一槽。
    let write_seq = ring.memory.u64_at(offsets::WRITE_SEQ);
    write_seq.store(3, Ordering::Relaxed);
    assert_eq!(reader.read_latest(&mut pixels, 3), Ok(ReadOutcome::Torn));
}

#[test]
fn reader_rejects_a_stable_slot_that_breaks_the_layout() {
    let ring = TestRing::new(2, 4, 4);
    let mut writer = FrameRingWriter::attach(&ring.params).unwrap();
    writer.write(&pattern_frame(1, 2, 2, 0)).unwrap();
    ring.memory
        .u32_at(ring.slot_start(0) + offset_of!(FrameSlotHeader, width))
        .store(5, Ordering::Relaxed);
    let reader = FrameRingReader::open(&ring.params).unwrap();
    let mut pixels = Vec::new();
    assert!(matches!(
        reader.read_latest(&mut pixels, 1),
        Err(ReadError::Corrupt(_))
    ));
}

/// 写方高速写、读方并发读：读到的帧要么完整一致，要么报撕裂，不能交出撕裂的帧。
#[test]
fn concurrent_reads_never_return_a_torn_frame() {
    const FRAMES: u64 = 20_000;
    let ring = TestRing::new(2, 64, 48);
    let params = ring.params.clone();
    let done = Arc::new(AtomicBool::new(false));
    let start = Arc::new(Barrier::new(2));
    let writer = {
        let (params, done, start) = (params.clone(), done.clone(), start.clone());
        thread::spawn(move || {
            let mut writer = FrameRingWriter::attach(&params).unwrap();
            start.wait();
            for number in 1..=FRAMES {
                // 尺寸随帧号变化，读方能从元数据推出整帧应有的内容。
                let width = 64 - u32::try_from(number % 7).unwrap();
                let height = 48 - u32::try_from(number % 5).unwrap();
                writer
                    .write(&pattern_frame(number, width, height, 4))
                    .unwrap();
            }
            done.store(true, Ordering::Release);
        })
    };
    let reader = FrameRingReader::open(&params).unwrap();
    let mut pixels = Vec::new();
    let (mut frames, mut torn) = (0u64, 0u64);
    start.wait();
    while !done.load(Ordering::Acquire) {
        match reader.read_latest(&mut pixels, 1).unwrap() {
            ReadOutcome::Empty => {}
            ReadOutcome::Torn => torn += 1,
            ReadOutcome::Frame(meta) => {
                let number = meta.frame_number;
                assert_eq!(meta.width, 64 - u32::try_from(number % 7).unwrap());
                assert_eq!(meta.height, 48 - u32::try_from(number % 5).unwrap());
                assert_eq!(meta.capture_ns, number * 1000 + 7);
                assert_eq!(meta.transform_version, number / 5 + 1);
                assert_eq!(meta.row_stride, meta.width * 4);
                let word = pattern_word(number);
                assert!(
                    pixels.as_chunks::<4>().0.iter().all(|pixel| *pixel == word),
                    "frame {number} is torn"
                );
                frames += 1;
            }
        }
    }
    writer.join().unwrap();
    assert!(
        frames > 0,
        "the reader never got a frame ({torn} torn reads)"
    );
    assert_eq!(read(&reader).0.frame_number, FRAMES);
}

fn fake_target(ring: &TestRing) -> FakeTarget {
    FakeTarget {
        surface: ring.params.clone(),
    }
}

#[test]
fn fake_capture_hands_over_the_latest_surface_frame() {
    let surface = TestRing::new(2, 8, 6);
    let mut display = FrameRingWriter::attach(&surface.params).unwrap();
    let mut capture = FakeCapture::open(&fake_target(&surface)).unwrap();
    let mut frame = Frame::default();
    assert_eq!(capture.next_frame(Duration::ZERO, &mut frame), Ok(false));
    assert_eq!(frame, Frame::default());

    for number in 1..=3 {
        display.write(&pattern_frame(number, 8, 6, 0)).unwrap();
    }
    assert_eq!(
        capture.next_frame(Duration::from_secs(5), &mut frame),
        Ok(true)
    );
    let expected = pattern_frame(3, 8, 6, 0);
    assert_eq!(
        frame.meta,
        FrameMeta {
            client_origin: Point { x: 0, y: 0 },
            dpi: FAKE_DPI,
            ..expected.meta
        }
    );
    assert_eq!(frame.pixels, expected.pixels);

    // 没有新帧：超时后返回 false，帧不变。
    assert_eq!(
        capture.next_frame(Duration::from_millis(20), &mut frame),
        Ok(false)
    );
    assert_eq!(frame.meta.number, 3);
    display.write(&pattern_frame(4, 5, 2, 8)).unwrap();
    assert_eq!(
        capture.next_frame(Duration::from_secs(5), &mut frame),
        Ok(true)
    );
    assert_eq!(
        (frame.meta.number, frame.meta.width, frame.meta.stride),
        (4, 5, 20)
    );
}

#[test]
fn frames_go_from_the_fake_display_through_fake_capture_into_the_frame_ring() {
    let surface = TestRing::new(2, 8, 6);
    let ring = TestRing::new(3, 8, 6);
    let mut display = FrameRingWriter::attach(&surface.params).unwrap();
    let mut capture = FakeCapture::open(&fake_target(&surface)).unwrap();
    let mut writer = FrameRingWriter::attach(&ring.params).unwrap();
    let reader = FrameRingReader::open(&ring.params).unwrap();
    let mut frame = Frame::default();
    for number in [1, 2, 5] {
        let shown = pattern_frame(number, 8 - u32::try_from(number).unwrap(), 6, 0);
        display.write(&shown).unwrap();
        assert_eq!(
            capture.next_frame(Duration::from_secs(5), &mut frame),
            Ok(true)
        );
        assert_eq!(writer.write(&frame), Ok(number));
        let (meta, pixels) = read(&reader);
        assert_eq!(
            (meta.frame_number, meta.capture_ns, meta.width),
            (number, shown.meta.captured_at_ns, shown.meta.width)
        );
        assert_eq!(pixels, shown.pixels);
    }
}

#[test]
fn fake_capture_reports_a_corrupt_surface() {
    let surface = TestRing::new(2, 4, 4);
    let mut display = FrameRingWriter::attach(&surface.params).unwrap();
    display.write(&pattern_frame(1, 2, 2, 0)).unwrap();
    surface
        .memory
        .u32_at(surface.slot_start(0) + offset_of!(FrameSlotHeader, pixel_format))
        .store(7, Ordering::Relaxed);
    let mut capture = FakeCapture::open(&fake_target(&surface)).unwrap();
    let mut frame = Frame::default();
    let error = capture
        .next_frame(Duration::from_secs(1), &mut frame)
        .unwrap_err();
    assert!(matches!(error, CaptureError::InvalidFrame(_)), "{error}");
    assert!(!error.is_recoverable());
}
