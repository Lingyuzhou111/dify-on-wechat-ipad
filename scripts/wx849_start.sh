#!/bin/bash

# 定义颜色
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m' # No Color

# 定义端口
REDIS_PORT=6379
PAD_PORT=9011

echo -e "${BLUE}+------------------------------------------------+${NC}"
echo -e "${BLUE}|         WX849 Protocol Service Starter         |${NC}"
echo -e "${BLUE}+------------------------------------------------+${NC}"

# 检查端口是否被占用
check_port() {
    local port=$1
    local service=$2
    if lsof -i :$port > /dev/null 2>&1; then
        echo -e "${RED}错误: $service 端口 $port 已被占用!${NC}"
        echo -e "${YELLOW}解决方案:${NC}"
        echo -e "1. 查看占用进程: ${BLUE}sudo lsof -i :$port${NC}"
        echo -e "2. 停止占用进程: ${BLUE}sudo kill -9 \$(sudo lsof -t -i :$port)${NC}"
        echo -e "3. 或者修改 $service 配置使用其他端口"
        return 1
    fi
    return 0
}

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# 第一步：启动Redis服务
echo -e "${YELLOW}[1/3] 正在启动Redis服务...${NC}"

# 检查Redis端口
if ! check_port $REDIS_PORT "Redis"; then
    exit 1
fi

cd $PROJECT_ROOT/lib/wx849/849/redis
redis-server redis.linux.conf &
REDIS_PID=$!

# 检查Redis是否启动成功
sleep 2
if ! ps -p $REDIS_PID > /dev/null; then
    echo -e "${RED}Redis启动失败! 请检查日志:${NC}"
    echo -e "${BLUE}cat $PROJECT_ROOT/lib/wx849/849/redis/redis.log${NC}"
    exit 1
fi

echo -e "${GREEN}Redis服务已启动，PID: $REDIS_PID${NC}"

# 第二步：启动PAD服务
echo -e "${YELLOW}[2/3] 正在启动 PAD 服务...${NC}"

# 检查PAD端口
if ! check_port $PAD_PORT "PAD"; then
    echo -e "${RED}正在关闭Redis服务...${NC}"
    kill $REDIS_PID
    exit 1
fi

# 读取配置文件中的协议版本
PROTOCOL_VERSION=$(grep "wx849_protocol_version" $PROJECT_ROOT/config.json | grep -o '"[0-9][0-9][0-9]"' | tr -d '"')

if [ -z "$PROTOCOL_VERSION" ]; then
    PROTOCOL_VERSION="849"
    echo -e "${YELLOW}未找到协议版本配置，使用默认版本: $PROTOCOL_VERSION${NC}"
else
    echo -e "${GREEN}使用配置的协议版本: $PROTOCOL_VERSION${NC}"
fi

# 根据协议版本选择不同的PAD目录
if [ "$PROTOCOL_VERSION" == "855" ]; then
    PAD_DIR="$PROJECT_ROOT/lib/wx849/849/pad2"
    echo -e "${BLUE}使用855协议 (pad2)${NC}"
elif [ "$PROTOCOL_VERSION" == "ipad" ]; then
    PAD_DIR="$PROJECT_ROOT/lib/wx849/849/pad3"
    echo -e "${BLUE}使用iPad协议 (pad3)${NC}"
else
    PAD_DIR="$PROJECT_ROOT/lib/wx849/849/pad"
    echo -e "${BLUE}使用849协议 (pad)${NC}"
fi

# 检查PAD目录是否存在
if [ ! -d "$PAD_DIR" ]; then
    echo -e "${RED}PAD目录 $PAD_DIR 不存在!${NC}"
    echo -e "${RED}正在关闭Redis服务...${NC}"
    kill $REDIS_PID
    exit 1
fi

# 启动PAD服务
cd $PAD_DIR
if [ -f "linuxService" ]; then
    chmod +x linuxService
    ./linuxService &
    PAD_PID=$!
elif [ -f "linuxService.exe" ]; then
    wine linuxService.exe &
    PAD_PID=$!
    echo -e "${GREEN}PAD服务已启动 (通过Wine)，PID: $PAD_PID${NC}"
else
    echo -e "${RED}找不到PAD服务可执行文件!${NC}"
    echo -e "${RED}正在关闭Redis服务...${NC}"
    kill $REDIS_PID
    exit 1
fi

# 检查PAD是否启动成功
sleep 3
if ! ps -p $PAD_PID > /dev/null; then
    echo -e "${RED}PAD服务启动失败! 可能原因:${NC}"
    echo -e "1. 端口冲突 (检查 $PAD_PORT 端口)"
    echo -e "2. 缺少依赖库"
    echo -e "3. 权限问题"
    echo -e "${YELLOW}请查看日志文件获取更多信息${NC}"
    echo -e "${RED}正在关闭Redis服务...${NC}"
    kill $REDIS_PID
    exit 1
fi

echo -e "${GREEN}PAD服务已启动，PID: $PAD_PID${NC}"

# 第三步：配置并启动主程序
echo -e "${YELLOW}[3/3] 配置完成，请扫描二维码登录微信...${NC}"
echo -e "${GREEN}WX849 协议服务已全部启动!${NC}"
echo
echo -e "${YELLOW}现在可以运行主程序，请确保在配置文件中设置:${NC}"
echo -e "${BLUE}  \"channel_type\": \"wx849\",${NC}"
echo -e "${BLUE}  \"wx849_protocol_version\": \"$PROTOCOL_VERSION\",${NC}"
echo -e "${BLUE}  \"wx849_api_host\": \"127.0.0.1\",${NC}"
echo -e "${BLUE}  \"wx849_api_port\": \"9000\"${NC}"
echo
echo -e "${YELLOW}提示: 如需停止WX849服务，请按Ctrl+C后运行 wx849_stop.sh 脚本${NC}"
echo -e "${BLUE}+------------------------------------------------+${NC}"

# 保持脚本运行
wait $PAD_PID
echo -e "${RED}PAD服务已停止，正在关闭Redis服务...${NC}"
kill $REDIS_PID
echo -e "${RED}所有服务已关闭${NC}"
