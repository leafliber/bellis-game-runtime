//! 偏移表：共享内存环的常量与 `#[repr(C)]` 结构的布局，帧环与音频环共用这一形式。
//!
//! 导出到 `protocol/` 的 `*-layout.json`，Python 手写的同一布局由测试逐项核对。

use std::sync::atomic::AtomicU64;

use serde::Serialize;

/// 偏移表中字段的类型。
pub(crate) trait FieldType {
    const NAME: &'static str;
}

impl FieldType for u32 {
    const NAME: &'static str = "u32";
}

impl FieldType for u64 {
    const NAME: &'static str = "u64";
}

impl FieldType for AtomicU64 {
    const NAME: &'static str = "atomic_u64";
}

/// 偏移表：常量与各结构的布局。
#[derive(Debug, Serialize)]
pub struct Layout {
    /// 布局常量，名字与模块中的常量相同。
    pub constants: Vec<Constant>,
    /// 各结构的布局。
    pub structs: Vec<StructLayout>,
}

/// 一个布局常量。
#[derive(Debug, Serialize)]
pub struct Constant {
    /// 常量名。
    pub name: &'static str,
    /// 常量值。
    pub value: u64,
}

/// 一个 `#[repr(C)]` 结构的布局。
#[derive(Debug, Serialize)]
pub struct StructLayout {
    /// 结构名。
    pub name: &'static str,
    /// `size_of`。
    pub size: usize,
    /// `align_of`。
    pub align: usize,
    /// 按声明顺序的字段。
    pub fields: Vec<FieldLayout>,
}

/// 一个字段的布局。
#[derive(Debug, Serialize)]
pub struct FieldLayout {
    /// 字段名。
    pub name: &'static str,
    /// `offset_of`。
    pub offset: usize,
    /// 字段的 `size_of`。
    pub size: usize,
    /// 字段类型：`u32`、`u64` 或 `atomic_u64`。
    #[serde(rename = "type")]
    pub ty: &'static str,
}

/// 列出结构的字段及其类型；类型与字段的实际类型不符时编译失败。
macro_rules! struct_layout {
    ($ty:ident { $($field:ident: $field_ty:ty),* $(,)? }) => {
        $crate::layout::StructLayout {
            name: stringify!($ty),
            size: ::std::mem::size_of::<$ty>(),
            align: ::std::mem::align_of::<$ty>(),
            fields: vec![$({
                let _: fn(&$ty) -> &$field_ty = |value| &value.$field;
                $crate::layout::FieldLayout {
                    name: stringify!($field),
                    offset: ::std::mem::offset_of!($ty, $field),
                    size: ::std::mem::size_of::<$field_ty>(),
                    ty: <$field_ty as $crate::layout::FieldType>::NAME,
                }
            }),*],
        }
    };
}

pub(crate) use struct_layout;

/// 偏移表须列出每个字段且没有隐式填充：字段按偏移首尾相接，最后一个字段止于结构末尾。
/// 这样两种语言都不依赖各自的填充规则，漏列字段也会失败。供各布局模块的测试调用。
#[cfg(test)]
pub(crate) fn assert_no_padding(layout: &Layout) {
    for layout in &layout.structs {
        let mut end = 0;
        for field in &layout.fields {
            assert_eq!(
                field.offset, end,
                "{}.{} leaves a gap",
                layout.name, field.name
            );
            assert_eq!(
                field.offset % field.size,
                0,
                "{}.{} is misaligned",
                layout.name,
                field.name
            );
            end = field.offset + field.size;
        }
        assert_eq!(
            end, layout.size,
            "{} has trailing padding or unlisted fields",
            layout.name
        );
        assert_eq!(layout.align, 8, "{}", layout.name);
    }
}
