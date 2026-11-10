"""把拆分后的处理器绑定到 AstrBot 的入口模块命名空间。

AstrBot 4.29 按处理器的 ``__module__`` 归属插件，而不看类继承关系。
因此本装饰器必须紧贴函数，写在 AstrBot 装饰器之下（更靠内）。
"""


def entry_handler(function):
    package = function.__module__.split('.handlers.', 1)[0]
    function.__module__ = package + '.main'
    return function
