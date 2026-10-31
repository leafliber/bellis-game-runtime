//! 单元测试：测试自己创建共享内存并写好头部（生产中由主进程做），再经写入方、读取方与假音频源读写。
//! 本进程对共享内存的访问全是原子操作，所以同进程的并发读写测试没有数据竞争。

use std::mem::offset_of;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Barrier};
use std::thread;
use std::time::Duration;

use audio_capture::{AudioError, AudioPacket, AudioSource};
use core_protocol::audio_ring::{
    AudioBlockHeader, BLOCK_ALIGN, BLOCK_SAMPLES_OFFSET, BLOCKS_OFFSET, FLAG_DISCONTINUITY,
    LAYOUT_VERSION, SAMPLE_FORMAT_F32,
};
use core_protocol::messages::{AudioRingAttachParams, FakeAudioTarget};
use shared_memory::{Created, SharedMemory};

use crate::ring::{header as offsets, write_header};
use crate::{
    AudioRingError, AudioRingReader, AudioRingWriter, BlockMeta, BlockRead, FakeAudioCapture,
    ReadError, WriteError,
};

fn unique_name() -> String {
    static NEXT: AtomicU32 = AtomicU32::new(0);
    let id = format!(
        "bellis-audio-ring-test-{}-{}",
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
    block_count: u32,
    block_capacity: u32,
    sample_rate: u32,
    channels: u32,
) -> AudioRingAttachParams {
    let block_stride = (BLOCK_SAMPLES_OFFSET + u64::from(block_capacity) * u64::from(channels) * 4)
        .next_multiple_of(BLOCK_ALIGN);
    AudioRingAttachParams {
        name,
        size_bytes: BLOCKS_OFFSET + u64::from(block_count) * block_stride,
        layout_version: LAYOUT_VERSION,
        block_count,
        block_stride,
        block_capacity,
        sample_rate,
        channels,
        sample_format: SAMPLE_FORMAT_F32,
    }
}

/// 测试创建的音频环：共享内存与写好的头部。
struct TestRing {
    params: AudioRingAttachParams,
    memory: SharedMemory,
    _created: Created,
}

impl TestRing {
    /// 48 kHz 立体声。
    fn new(block_count: u32, block_capacity: u32) -> Self {
        Self::with_format(block_count, block_capacity, 48_000, 2)
    }

    fn with_format(block_count: u32, block_capacity: u32, sample_rate: u32, channels: u32) -> Self {
        let params = ring_params(
            unique_name(),
            block_count,
            block_capacity,
            sample_rate,
            channels,
        );
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
        usize::try_from(BLOCKS_OFFSET + u64::from(slot) * self.params.block_stride).unwrap()
    }

    fn fake(&self) -> FakeAudioTarget {
        FakeAudioTarget {
            output: self.params.clone(),
        }
    }
}

/// 立体声的包：第 i 个采样点左声道为 `base + i`，右声道为它的相反数。
fn packet(base: f32, sample_count: u32, capture_ns: u64, discontinuity: bool) -> AudioPacket {
    let mut data = Vec::with_capacity(sample_count as usize * 8);
    for i in 0..sample_count {
        #[allow(clippy::cast_precision_loss, reason = "测试用的小整数")]
        let left = base + i as f32;
        data.extend_from_slice(&left.to_ne_bytes());
        data.extend_from_slice(&(-left).to_ne_bytes());
    }
    AudioPacket {
        capture_ns,
        sample_count,
        data,
        discontinuity,
    }
}

fn read(reader: &AudioRingReader, number: u64) -> (BlockMeta, Vec<u8>) {
    let mut samples = Vec::new();
    match reader.read_block(number, &mut samples).unwrap() {
        BlockRead::Block(meta) => (meta, samples),
        other => panic!("block {number}: {other:?}"),
    }
}

#[test]
fn a_written_packet_reads_back_as_one_block() {
    let ring = TestRing::new(4, 480);
    let mut writer = AudioRingWriter::attach(&ring.params).unwrap();
    let reader = AudioRingReader::open(&ring.params).unwrap();
    assert_eq!(reader.latest_block_number(), 0);
    let sent = packet(1.0, 480, 5_000_000, false);
    assert_eq!(writer.write(&sent).unwrap().block_number, 1);
    assert_eq!(reader.latest_block_number(), 1);
    let (meta, samples) = read(&reader, 1);
    assert_eq!(
        meta,
        BlockMeta {
            block_number: 1,
            capture_ns: 5_000_000,
            sample_count: 480,
            flags: 0,
        }
    );
    assert_eq!(samples, sent.data);
    assert_eq!(writer.format().bytes_per_sample(), 8);
}

#[test]
fn a_long_packet_is_split_and_later_blocks_are_timed_from_the_sample_rate() {
    let ring = TestRing::new(8, 200);
    let mut writer = AudioRingWriter::attach(&ring.params).unwrap();
    let reader = AudioRingReader::open(&ring.params).unwrap();
    let sent = packet(0.0, 480, 1_000_000_000, true);
    assert_eq!(writer.write(&sent).unwrap().block_number, 3);
    let blocks: Vec<_> = (1..=3).map(|n| read(&reader, n)).collect();
    let counts: Vec<u32> = blocks.iter().map(|(meta, _)| meta.sample_count).collect();
    assert_eq!(counts, [200, 200, 80]);
    // 48 kHz：200 个采样点是 4_166_666.67 ns，向下取整。
    let times: Vec<u64> = blocks.iter().map(|(meta, _)| meta.capture_ns).collect();
    assert_eq!(times, [1_000_000_000, 1_004_166_666, 1_008_333_333]);
    let flags: Vec<u32> = blocks.iter().map(|(meta, _)| meta.flags).collect();
    assert_eq!(flags, [FLAG_DISCONTINUITY, 0, 0]);
    let joined: Vec<u8> = blocks
        .iter()
        .flat_map(|(_, samples)| samples.clone())
        .collect();
    assert_eq!(joined, sent.data);
}

#[test]
fn packets_that_break_the_rules_are_not_written() {
    let ring = TestRing::new(4, 480);
    let mut writer = AudioRingWriter::attach(&ring.params).unwrap();
    let reader = AudioRingReader::open(&ring.params).unwrap();
    let empty = AudioPacket::default();
    assert!(matches!(
        writer.write(&empty),
        Err(WriteError::InvalidPacket(_))
    ));
    let mut short = packet(0.0, 10, 1, false);
    short.data.pop();
    assert!(matches!(
        writer.write(&short),
        Err(WriteError::InvalidPacket(_))
    ));
    assert_eq!(reader.latest_block_number(), 0);
    assert_eq!(writer.last_block_number(), 0);
}

#[test]
fn block_numbers_continue_after_a_restart() {
    let ring = TestRing::new(4, 480);
    {
        let mut writer = AudioRingWriter::attach(&ring.params).unwrap();
        for i in 0..3 {
            writer
                .write(&packet(0.0, 100, 1_000 * (i + 1), false))
                .unwrap();
        }
    }
    let mut writer = AudioRingWriter::attach(&ring.params).unwrap();
    assert_eq!(writer.last_block_number(), 3);
    assert_eq!(
        writer
            .write(&packet(0.0, 100, 9_000, true))
            .unwrap()
            .block_number,
        4
    );
}

#[test]
fn a_seq_left_odd_by_a_crash_is_skipped_to_the_next_odd() {
    let ring = TestRing::new(2, 16);
    let seq_at = ring.slot_start(0) + offset_of!(AudioBlockHeader, seq);
    ring.memory.u64_at(seq_at).store(7, Ordering::Relaxed);
    let mut writer = AudioRingWriter::attach(&ring.params).unwrap();
    writer.write(&packet(0.0, 16, 1, false)).unwrap();
    assert_eq!(ring.memory.u64_at(seq_at).load(Ordering::Relaxed), 10);
    let reader = AudioRingReader::open(&ring.params).unwrap();
    assert_eq!(read(&reader, 1).0.sample_count, 16);
}

#[test]
fn torn_overwritten_and_corrupt_blocks_are_told_apart() {
    let ring = TestRing::new(2, 16);
    let mut writer = AudioRingWriter::attach(&ring.params).unwrap();
    let reader = AudioRingReader::open(&ring.params).unwrap();
    for i in 0..3 {
        writer.write(&packet(0.0, 16, i + 1, false)).unwrap();
    }
    let mut samples = Vec::new();
    // 块 1 已被块 3 覆盖。
    assert_eq!(
        reader.read_block(1, &mut samples).unwrap(),
        BlockRead::Missing
    );
    // 块 4 还没写。
    assert_eq!(
        reader.read_block(4, &mut samples).unwrap(),
        BlockRead::Missing
    );
    // 写方正在写块 2 的槽。
    let seq_at = ring.slot_start(1) + offset_of!(AudioBlockHeader, seq);
    let seq = ring.memory.u64_at(seq_at);
    let even = seq.load(Ordering::Relaxed);
    seq.store(even + 1, Ordering::Relaxed);
    assert_eq!(reader.read_block(2, &mut samples).unwrap(), BlockRead::Torn);
    seq.store(even, Ordering::Relaxed);
    // 稳定却不合布局：采样点数超过容量。
    let count_at = ring.slot_start(1) + offset_of!(AudioBlockHeader, sample_count);
    ring.memory.u32_at(count_at).store(17, Ordering::Relaxed);
    assert!(matches!(
        reader.read_block(2, &mut samples),
        Err(ReadError::Corrupt(_))
    ));
    ring.memory.u32_at(count_at).store(16, Ordering::Relaxed);
    let flags_at = ring.slot_start(1) + offset_of!(AudioBlockHeader, flags);
    ring.memory.u32_at(flags_at).store(0x10, Ordering::Relaxed);
    assert!(matches!(
        reader.read_block(2, &mut samples),
        Err(ReadError::Corrupt(_))
    ));
}

/// 改坏参数的一种方式。
type Change = fn(&mut AudioRingAttachParams);

#[test]
fn parameters_are_checked_against_the_layout_rules() {
    let good = ring_params(unique_name(), 4, 480, 48_000, 2);
    let cases: [(&str, Change); 9] = [
        ("empty name", |p| p.name.clear()),
        ("layout version", |p| p.layout_version += 1),
        ("one block", |p| p.block_count = 1),
        ("sample format", |p| p.sample_format = 2),
        ("no channels", |p| p.channels = 0),
        ("three channels", |p| p.channels = 3),
        ("sample rate", |p| p.sample_rate = 7_999),
        ("stride alignment", |p| p.block_stride += 1),
        ("size", |p| p.size_bytes -= 1),
    ];
    for (what, change) in cases {
        let mut params = good.clone();
        change(&mut params);
        assert!(
            matches!(
                AudioRingWriter::attach(&params),
                Err(AudioRingError::InvalidParams(_))
            ),
            "{what}"
        );
    }
    let mut params = good.clone();
    params.block_capacity = 0;
    assert!(matches!(
        AudioRingWriter::attach(&params),
        Err(AudioRingError::InvalidParams(_))
    ));
    let mut params = good;
    params.block_capacity = 481;
    assert!(matches!(
        AudioRingWriter::attach(&params),
        Err(AudioRingError::InvalidParams(_))
    ));
}

#[test]
fn missing_or_small_shared_memory_is_refused() {
    let params = ring_params(unique_name(), 4, 480, 48_000, 2);
    assert!(matches!(
        AudioRingWriter::attach(&params),
        Err(AudioRingError::Open { .. })
    ));
    let _created = Created::new(&params.name, 64 * 1024).unwrap();
    let mut large = params;
    large.block_count = 64;
    large.size_bytes = BLOCKS_OFFSET + 64 * large.block_stride;
    assert!(matches!(
        AudioRingWriter::attach(&large),
        Err(AudioRingError::Open { .. })
    ));
}

#[test]
fn the_header_must_match_the_parameters_field_by_field() {
    let ring = TestRing::new(4, 480);
    let checks: [(&str, usize, bool); 7] = [
        ("layout_version", offsets::LAYOUT_VERSION, false),
        ("block_count", offsets::BLOCK_COUNT, false),
        ("block_stride", offsets::BLOCK_STRIDE, true),
        ("block_capacity", offsets::BLOCK_CAPACITY, false),
        ("sample_rate", offsets::SAMPLE_RATE, false),
        ("channels", offsets::CHANNELS, false),
        ("sample_format", offsets::SAMPLE_FORMAT, false),
    ];
    for (name, offset, wide) in checks {
        let restore = if wide {
            let value = ring.memory.u64_at(offset).load(Ordering::Relaxed);
            ring.memory
                .u64_at(offset)
                .store(value + 64, Ordering::Relaxed);
            value
        } else {
            let value = ring.memory.u32_at(offset).load(Ordering::Relaxed);
            ring.memory
                .u32_at(offset)
                .store(value + 1, Ordering::Relaxed);
            u64::from(value)
        };
        match AudioRingWriter::attach(&ring.params) {
            Err(AudioRingError::Mismatch { field, .. }) => assert_eq!(field, name),
            other => panic!("{name}: {other:?}"),
        }
        if wide {
            ring.memory.u64_at(offset).store(restore, Ordering::Relaxed);
        } else {
            let value = u32::try_from(restore).unwrap();
            ring.memory.u32_at(offset).store(value, Ordering::Relaxed);
        }
    }
    ring.memory
        .u64_at(offsets::MAGIC)
        .store(1, Ordering::Relaxed);
    assert!(matches!(
        AudioRingWriter::attach(&ring.params),
        Err(AudioRingError::Mismatch { field: "magic", .. })
    ));
}

/// 写方高速写、读方并发读：读到的块要么完整一致，要么报撕裂或已被覆盖。
#[test]
fn concurrent_reads_never_see_a_torn_block() {
    let ring = TestRing::new(3, 64);
    let mut writer = AudioRingWriter::attach(&ring.params).unwrap();
    let reader = AudioRingReader::open(&ring.params).unwrap();
    let done = Arc::new(AtomicBool::new(false));
    let start = Arc::new(Barrier::new(2));
    let checker = {
        let (done, start) = (Arc::clone(&done), Arc::clone(&start));
        thread::spawn(move || {
            start.wait();
            let mut samples = Vec::new();
            let mut complete = 0u64;
            while !done.load(Ordering::Acquire) {
                let latest = reader.latest_block_number();
                if latest == 0 {
                    continue;
                }
                if let BlockRead::Block(meta) = reader.read_block(latest, &mut samples).unwrap() {
                    assert_eq!(meta.capture_ns, meta.block_number * 1_000);
                    #[allow(clippy::cast_precision_loss, reason = "测试用的小整数")]
                    let expected = packet(meta.block_number as f32, meta.sample_count, 0, false);
                    assert_eq!(samples, expected.data, "block {}", meta.block_number);
                    complete += 1;
                }
            }
            complete
        })
    };
    start.wait();
    for number in 1..=20_000u64 {
        let count = u32::try_from(number % 64).unwrap() + 1;
        #[allow(clippy::cast_precision_loss, reason = "测试用的小整数")]
        writer
            .write(&packet(number as f32, count, number * 1_000, false))
            .unwrap();
    }
    done.store(true, Ordering::Release);
    assert!(checker.join().unwrap() > 0);
}

fn play(writer: &mut AudioRingWriter, number: u64, discontinuity: bool) {
    #[allow(clippy::cast_precision_loss, reason = "测试用的小整数")]
    writer
        .write(&packet(
            number as f32 * 10.0,
            10,
            number * 1_000,
            discontinuity,
        ))
        .unwrap();
}

#[test]
fn the_fake_source_hands_over_every_new_block_in_order() {
    let output = TestRing::new(8, 16);
    let mut speaker = AudioRingWriter::attach(&output.params).unwrap();
    play(&mut speaker, 1, false);
    // 打开之前写入的声音不交出，与真实的音频源一样。
    let mut source = FakeAudioCapture::open(&output.fake()).unwrap();
    assert_eq!(source.format(), speaker.format());
    let mut got = AudioPacket::default();
    assert!(
        !source
            .next_packet(Duration::from_millis(5), &mut got)
            .unwrap()
    );
    assert_eq!(got, AudioPacket::default());
    for number in 2..=4 {
        play(&mut speaker, number, number == 3);
    }
    for number in 2..=4 {
        assert!(
            source
                .next_packet(Duration::from_millis(100), &mut got)
                .unwrap()
        );
        #[allow(clippy::cast_precision_loss, reason = "测试用的小整数")]
        let expected = packet(number as f32 * 10.0, 10, number * 1_000, number == 3);
        assert_eq!(got, expected);
    }
    assert!(
        !source
            .next_packet(Duration::from_millis(5), &mut got)
            .unwrap()
    );
}

#[test]
fn the_fake_source_skips_overwritten_blocks_and_marks_the_gap() {
    let output = TestRing::new(4, 16);
    let mut speaker = AudioRingWriter::attach(&output.params).unwrap();
    let mut source = FakeAudioCapture::open(&output.fake()).unwrap();
    for number in 1..=10 {
        play(&mut speaker, number, false);
    }
    // 块 1 到 6 已被覆盖：从仍在的块 7 起交出，第一个标不连续。
    let mut got = AudioPacket::default();
    for number in 7..=10 {
        assert!(
            source
                .next_packet(Duration::from_millis(100), &mut got)
                .unwrap()
        );
        assert_eq!(got.capture_ns, number * 1_000);
        assert_eq!(got.discontinuity, number == 7, "block {number}");
    }
}

#[test]
fn a_corrupt_block_on_the_fake_output_is_an_error() {
    let output = TestRing::new(4, 16);
    let mut speaker = AudioRingWriter::attach(&output.params).unwrap();
    let mut source = FakeAudioCapture::open(&output.fake()).unwrap();
    play(&mut speaker, 1, false);
    let flags_at = output.slot_start(0) + offset_of!(AudioBlockHeader, flags);
    output
        .memory
        .u32_at(flags_at)
        .store(0x80, Ordering::Relaxed);
    let mut got = AudioPacket::default();
    assert!(matches!(
        source.next_packet(Duration::from_millis(100), &mut got),
        Err(AudioError::InvalidBlock(_))
    ));
}

#[test]
fn the_fake_source_reports_the_output_format() {
    let output = TestRing::with_format(4, 160, 16_000, 1);
    let source = FakeAudioCapture::open(&output.fake()).unwrap();
    let format = source.format();
    assert_eq!((format.sample_rate, format.channels), (16_000, 1));
}
