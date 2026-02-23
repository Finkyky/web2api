import datetime
import pytz


def convert_timestamp_to_time(
    timestamp, source_tz_str, target_tz_str, output_format="%Y/%m/%d %H:%M"
):
    """
    将Unix时间戳从源时区转换为目标时区的格式化时间
    :param timestamp: Unix时间戳（秒），如1770836400
    :param source_tz_str: 源时区字符串，如"America/Chicago"
    :param target_tz_str: 目标时区字符串，如"Asia/Shanghai"
    :param output_format: 输出时间格式，默认"%Y/%m/%d %H:%M"
    :return: 格式化后的目标时区时间字符串
    """
    try:
        # 1. 定义源时区和目标时区对象
        # source_tz = pytz.timezone(source_tz_str)
        target_tz = pytz.timezone(target_tz_str)

        # 2. 将时间戳转换为UTC时间（规避夏令时直接计算的问题）
        utc_time = datetime.datetime.fromtimestamp(int(timestamp), pytz.UTC)

        # 3. 先转换到源时区（验证用，也可省略），再转换到目标时区
        # source_time = utc_time.astimezone(source_tz)
        target_time = utc_time.astimezone(target_tz)

        # 4. 格式化输出目标时间
        return target_time.strftime(output_format)

    except Exception as e:
        return f"转换失败：{str(e)}"
