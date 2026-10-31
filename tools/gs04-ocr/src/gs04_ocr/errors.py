class EngineUnavailableError(Exception):
    """引擎在本机不可用：模型不在本地数据目录、不是 Windows、没装可选依赖或没有语言组件。"""
