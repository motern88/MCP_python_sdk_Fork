import asyncio
import json
import logging
import os
import shutil
from contextlib import AsyncExitStack
from typing import Any

import httpx
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# 配置日志记录
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)  # 设置日志记录的基本配置，日志级别为INFO，日志格式包括时间、日志级别和日志消息


class Configuration:
    """管理MCP客户端的配置和环境变量。"""

    def __init__(self) -> None:
        """使用环境变量初始化配置。"""
        self.load_env()  # 调用load_env方法加载环境变量
        self.api_key = os.getenv("LLM_API_KEY")  # 从环境变量中获取LLM_API_KEY的值并赋给self.api_key

    @staticmethod
    def load_env() -> None:
        """从.env文件加载环境变量。"""
        load_dotenv()  # 调用load_dotenv函数加载.env文件中的环境变量

    @staticmethod
    def load_config(file_path: str) -> dict[str, Any]:
        """从JSON文件加载服务器配置。

        参数：
            file_path: JSON配置文件的路径。

        返回：
            包含服务器配置的字典。

        引发异常：
            FileNotFoundError: 如果配置文件不存在。
            JSONDecodeError: 如果配置文件不是有效的JSON。
        """
        with open(file_path, "r") as f:  # 打开指定路径的文件
            return json.load(f)  # 将文件内容加载为JSON格式并返回

    @property
    def llm_api_key(self) -> str:
        """获取LLM API密钥。

        返回：
            API密钥作为字符串。

        引发异常：
            ValueError: 如果环境变量中未找到API密钥。
        """
        if not self.api_key:  # 如果self.api_key为空
            raise ValueError("LLM_API_KEY not found in environment variables")  # 抛出ValueError异常
        return self.api_key  # 返回API密钥


class Server:
    """管理MCP服务器连接和工具执行。"""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        """初始化服务器对象。

        参数：
            name: 服务器名称。
            config: 服务器配置。
        """
        self.name: str = name  # 服务器名称
        self.config: dict[str, Any] = config  # 服务器配置
        self.stdio_context: Any | None = None  # 标准输入输出上下文
        self.session: ClientSession | None = None  # 客户端会话
        self._cleanup_lock: asyncio.Lock = asyncio.Lock()  # 清理锁
        self.exit_stack: AsyncExitStack = AsyncExitStack()  # 异步退出栈

    async def initialize(self) -> None:
        """初始化服务器连接。"""
        command = (
            shutil.which("npx")
            if self.config["command"] == "npx"
            else self.config["command"]
        )  # 根据配置确定命令，如果是'npx'，使用shutil.which找到'npx'的实际路径
        if command is None:
            raise ValueError("The command must be a valid string and cannot be None.")  # 如果命令为空，抛出异常

        server_params = StdioServerParameters(
            command=command,
            args=self.config["args"],
            env={**os.environ, **self.config["env"]}
            if self.config.get("env")
            else None,
        )  # 创建服务器参数对象，包括命令、参数和环境变量
        try:
            stdio_transport = await self.exit_stack.enter_async_context(
                stdio_client(server_params)
            )  # 使用异步上下文管理器创建标准输入输出传输
            read, write = stdio_transport  # 获取读写流
            session = await self.exit_stack.enter_async_context(
                ClientSession(read, write)
            )  # 创建客户端会话
            await session.initialize()  # 初始化会话
            self.session = session  # 将会话赋值给
        except Exception as e:
            logging.error(f"Error initializing server {self.name}: {e}")  # 记录初始化错误
            await self.cleanup()  # 清理资源
            raise  # 抛出异常

    async def list_tools(self) -> list[Any]:
        """列出服务器上的可用工具。

        返回：
            可用工具列表。

        引发异常：
            RuntimeError: 如果服务器未初始化。
        """
        if not self.session:
            raise RuntimeError(f"Server {self.name} not initialized")  # 如果会话为空，抛出异常

        tools_response = await self.session.list_tools() # 从会话中获取工具列表响应
        tools = []  # 初始化工具列表

        for item in tools_response:
            if isinstance(item, tuple) and item[0] == "tools":  # 如果响应项是工具列表
                tools.extend(
                    Tool(tool.name, tool.description, tool.inputSchema, tool.title)
                    for tool in item[1]
                )  # 将工具对象添加到工具列表

        return tools  # 返回工具列表

    async def execute_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        retries: int = 2,
        delay: float = 1.0,
    ) -> Any:
        """执行工具并提供重试机制。

        参数：
            tool_name: 要执行的工具名称。
            arguments: 工具的参数。
            retries: 重试次数。
            delay: 重试之间的延迟时间（秒）。

        返回：
            工具执行的结果。

        引发异常：
            RuntimeError: 如果服务器未初始化。
            Exception: 如果在所有重试后工具执行失败。
        """
        if not self.session:  # 如果会话未初始化
            raise RuntimeError(f"Server {self.name} not initialized")  # 抛出异常

        attempt = 0  # 初始化尝试次数
        while attempt < retries:  # 当尝试次数小于重试次数时
            try:
                logging.info(f"Executing {tool_name}...")  # 记录工具执行信息
                result = await self.session.call_tool(tool_name, arguments)  # 调用工具并获取结果

                return result  # 返回工具执行结果

            except Exception as e:  # 捕获异常
                attempt += 1  # 增加尝试次数
                logging.warning(
                    f"Error executing tool: {e}. Attempt {attempt} of {retries}."
                )  # 记录工具执行错误和尝试次数
                if attempt < retries:  # 如果还有重试机会
                    logging.info(f"Retrying in {delay} seconds...")  # 记录重试延迟信息
                    await asyncio.sleep(delay)  # 等待延迟时间
                else:  # 如果没有重试机会
                    logging.error("Max retries reached. Failing.")  # 记录达到最大重试次数的信息
                    raise  # 抛出异常

    async def cleanup(self) -> None:
        """清理服务器资源。"""
        async with self._cleanup_lock:  # 使用异步锁确保清理过程的线程安全
            try:
                await self.exit_stack.aclose()  # 关闭异步退出栈
                self.session = None  # 清空会话
                self.stdio_context = None  # 清空标准输入输出上下文
            except Exception as e:  # 捕获清理过程中的异常
                logging.error(f"Error during cleanup of server {self.name}: {e}")  # 记录清理错误信息


class Tool:
    """表示一个工具及其属性和格式化信息。"""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        title: str | None = None,
    ) -> None:
        """初始化工具对象。

        参数：
            name: 工具名称。
            description: 工具描述。
            input_schema: 工具输入参数的JSON模式。
            title: 工具的用户可读标题（可选）。
        """
        self.name: str = name  # 工具名称
        self.title: str | None = title  # 工具的用户可读标题
        self.description: str = description  # 工具描述
        self.input_schema: dict[str, Any] = input_schema  # 工具输入参数的JSON模式

    def format_for_llm(self) -> str:
        """将工具信息格式化为LLM（语言模型）可理解的格式。

        返回：
            描述工具的格式化字符串。
        """
        args_desc = []  # 初始化参数描述列表
        if "properties" in self.input_schema:  # 如果输入模式中包含属性
            for param_name, param_info in self.input_schema["properties"].items():  # 遍历每个参数
                arg_desc = (
                    f"- {param_name}: {param_info.get('description', 'No description')}"
                )  # 构造参数描述
                if param_name in self.input_schema.get("required", []):  # 如果参数是必需的
                    arg_desc += " (required)"  # 添加“必需”标记
                args_desc.append(arg_desc)  # 将参数描述添加到列表

        # 构造格式化输出，标题作为单独字段
        output = f"Tool: {self.name}\n"  # 添加工具名称

        # 如果有用户可读标题，则添加
        if self.title:
            output += f"User-readable title: {self.title}\n"  # 添加用户可读标题

        output += f"""Description: {self.description}
        Arguments:
        {chr(10).join(args_desc)}
        """  # 添加工具描述和参数描述

        return output  # 返回格式化后的字符串


class LLMClient:
    """管理与LLM（语言模型）提供商的通信。"""

    def __init__(self, api_key: str) -> None:
        """初始化LLM客户端。

        参数：
            api_key: LLM提供商的API密钥。
        """
        self.api_key: str = api_key  # LLM提供商的API密钥

    def get_response(self, messages: list[dict[str, str]]) -> str:
        """从LLM获取响应。

        参数：
            messages: 消息字典列表，包含与LLM的对话历史。

        返回：
            LLM的响应，作为字符串。

        引发异常：
            httpx.RequestError: 如果向LLM的请求失败。
        """
        url = "https://api.groq.com/openai/v1/chat/completions"  # LLM的API端点

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }  # 设置请求头，包括内容类型和授权令牌
        payload = {
            "messages": messages,
            "model": "meta-llama/llama-4-scout-17b-16e-instruct",
            "temperature": 0.7,
            "max_tokens": 4096,
            "top_p": 1,
            "stream": False,
            "stop": None,
        }  # 构造请求负载，包括消息、模型选择和其他参数

        try:
            with httpx.Client() as client:  # 创建一个httpx客户端
                response = client.post(url, headers=headers, json=payload)  # 发送POST请求
                response.raise_for_status()  # 如果响应状态码不是200，抛出异常
                data = response.json()  # 解析响应为JSON
                return data["choices"][0]["message"]["content"]  # 返回LLM的响应内容

        except httpx.RequestError as e:  # 捕获请求异常
            error_message = f"Error getting LLM response: {str(e)}"  # 构造错误信息
            logging.error(error_message)  # 记录错误信息

            if isinstance(e, httpx.HTTPStatusError):  # 如果是HTTP状态错误
                status_code = e.response.status_code  # 获取状态码
                logging.error(f"Status code: {status_code}")  # 记录状态码
                logging.error(f"Response details: {e.response.text}")  # 记录响应详情

            return (
                f"I encountered an error: {error_message}. "
                "Please try again or rephrase your request."
            )  # 返回错误信息，提示用户重试或重新表述请求


class ChatSession:
    """协调用户、LLM和工具之间的交互。"""

    def __init__(self, servers: list[Server], llm_client: LLMClient) -> None:
        """初始化聊天会话。

        参数：
            servers: 服务器列表。
            llm_client: LLM客户端。
        """
        self.servers: list[Server] = servers  # 服务器列表
        self.llm_client: LLMClient = llm_client  # LLM客户端

    async def cleanup_servers(self) -> None:
        """正确清理所有服务器。"""
        for server in reversed(self.servers):  # 逆序遍历服务器列表
            try:
                await server.cleanup()  # 调用服务器的清理方法
            except Exception as e:  # 捕获清理过程中可能出现的异常
                logging.warning(f"Warning during final cleanup: {e}")  # 记录警告信息

    async def process_llm_response(self, llm_response: str) -> str:
        """处理LLM的响应，并在需要时执行工具。

        参数：
            llm_response: LLM的响应。

        返回：
            工具执行的结果或原始响应。
        """
        import json  # 导入json模块

        try:
            tool_call = json.loads(llm_response)  # 尝试将LLM响应解析为JSON
            if "tool" in tool_call and "arguments" in tool_call:  # 如果响应中包含工具调用信息
                logging.info(f"Executing tool: {tool_call['tool']}")  # 记录正在执行的工具名称
                logging.info(f"With arguments: {tool_call['arguments']}")  # 记录工具的参数

                for server in self.servers:  # 遍历服务器列表
                    tools = await server.list_tools()  # 获取服务器上的工具列表
                    if any(tool.name == tool_call["tool"] for tool in tools):  # 如果找到匹配的工具
                        try:
                            result = await server.execute_tool(
                                tool_call["tool"], tool_call["arguments"]
                            )  # 调用工具并获取结果

                            if isinstance(result, dict) and "progress" in result:  # 如果结果包含进度信息
                                progress = result["progress"]  # 获取进度值
                                total = result["total"]  # 获取总值
                                percentage = (progress / total) * 100  # 计算百分比
                                logging.info(
                                    f"Progress: {progress}/{total} ({percentage:.1f}%)"
                                )  # 记录进度信息

                            return f"Tool execution result: {result}"  # 返回工具执行结果
                        except Exception as e:  # 捕获工具执行过程中可能出现的异常
                            error_msg = f"Error executing tool: {str(e)}"  # 构造错误信息
                            logging.error(error_msg)  # 记录错误信息
                            return error_msg  # 返回错误信息

                return f"No server found with tool: {tool_call['tool']}"  # 如果未找到匹配的工具，返回提示信息
            return llm_response  # 如果响应不是工具调用信息，直接返回原始响应
        except json.JSONDecodeError:  # 如果解析JSON失败
            return llm_response  # 直接返回原始响应

    async def start(self) -> None:
        """主聊天会话处理器。"""
        try:
            for server in self.servers:  # 遍历服务器列表
                try:
                    await server.initialize()  # 初始化服务器
                except Exception as e:  # 捕获初始化过程中可能出现的异常
                    logging.error(f"Failed to initialize server: {e}")  # 记录错误信息
                    await self.cleanup_servers()  # 清理服务器资源
                    return  # 退出程序

            all_tools = []  # 初始化工具列表
            for server in self.servers:  # 遍历服务器列表
                tools = await server.list_tools()  # 获取服务器上的工具列表
                all_tools.extend(tools)  # 将工具添加到总列表

            tools_description = "\n".join([tool.format_for_llm() for tool in all_tools])  # 生成工具描述信息

            system_message = (
                "您是一位有帮助的助手，可以使用以下工具：\n\n"
                f"{tools_description}\n"
                "根据用户的问题选择合适的工具。 "
                "如果不需要工具，直接回复即可。\n\n"
                "重要提示：当您需要使用工具时，您必须仅以以下精确的JSON对象格式回复，不能有其他内容：\n"
                "{\n"
                '    "tool": "tool-name",\n'
                '    "arguments": {\n'
                '        "argument-name": "value"\n'
                "    }\n"
                "}\n\n"
                "在收到工具的响应后：\n"
                "1. 将原始数据转换为自然、对话式的回应。\n"
                "2. 保持回应简洁但信息丰富\n"
                "3. 关注最相关的信息。\n"
                "4. 使用用户问题中的适当上下文。\n"
                "5. 避免简单重复原始数据。\n\n"
                "请仅使用上述明确定义的工具。"
            )  # 构造系统消息

            messages = [{"role": "system", "content": system_message}]  # 初始化消息列表

            while True:
                try:
                    user_input = input("You: ").strip().lower()  # 获取用户输入
                    if user_input in ["quit", "exit"]:  # 如果用户输入退出命令
                        logging.info("\nExiting...")  # 记录退出信息
                        break  # 退出循环

                    messages.append({"role": "user", "content": user_input})  # 将用户输入添加到消息列表

                    llm_response = self.llm_client.get_response(messages)  # 获取LLM的响应
                    logging.info("\nAssistant: %s", llm_response)  # 记录LLM的响应

                    result = await self.process_llm_response(llm_response)  # 处理LLM的响应

                    if result != llm_response:  # 如果处理结果与原始响应不同
                        messages.append({"role": "assistant", "content": llm_response})  # 将原始响应添加到消息列表
                        messages.append({"role": "system", "content": result})  # 将处理结果添加到消息列表

                        final_response = self.llm_client.get_response(messages)  # 获取最终响应
                        logging.info("\nFinal response: %s", final_response)  # 记录最终响应
                        messages.append(
                            {"role": "assistant", "content": final_response}
                        )  # 将最终响应添加到消息列表
                    else:
                        messages.append({"role": "assistant", "content": llm_response})  # 将原始响应添加到消息列表

                except KeyboardInterrupt:  # 捕获键盘中断异常
                    logging.info("\nExiting...")  # 记录退出信息
                    break  # 退出循环

        finally:
            await self.cleanup_servers()  # 最终清理服务器资源


async def main() -> None:
    """初始化并运行聊天会话。"""
    config = Configuration()  # 创建配置对象
    server_config = config.load_config("servers_config.json")  # 加载服务器配置文件
    servers = [
        Server(name, srv_config)
        for name, srv_config in server_config["mcpServers"].items()
    ]  # 根据配置创建服务器对象列表
    llm_client = LLMClient(config.llm_api_key)  # 创建LLM客户端对象
    chat_session = ChatSession(servers, llm_client)  # 创建聊天会话对象
    await chat_session.start()  # 启动聊天会话


if __name__ == "__main__":
    asyncio.run(main())  # 如果是主模块，运行main函数
