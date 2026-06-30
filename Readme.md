# <center>RealTime-PPP软件说明1.0</center>

&emsp;&emsp;本软件基于Python代码开发，主要用途为实时接收RTCM数据流进行PPP解算得到实时PWV。

## 1.文件介绍

**根目录**：
&emsp;&emsp;1）brdm_downloader.py：下载实时BKG、IGS广播星历文件；
&emsp;&emsp;2）sp3_downloader.py，下载WUM、GRG近实时精密星历文件，预报一天；
&emsp;&emsp;3）rt_stream32_lowcpu2.py：实时数据流接收；
&emsp;&emsp;4）str2str.exe：数据流接收程序；
&emsp;&emsp;5）controller_master.py：进程总控；
&emsp;&emsp;6）Data_clean.py：数据动态清理；
&emsp;&emsp;7）controller_master.py：进程总控；
&emsp;&emsp;8）start\end PPP.bat：一键起算、结束脚本；
&emsp;&emsp;9）controller_master.py：进程总控；
**子目录**：
&emsp;&emsp;1）jiema\chushihua1.conf：解算配置文件；
&emsp;&emsp;2）path_config：路径配置文件；
&emsp;&emsp;3）readtime_ppp_controller.py：PPP解算代码；
&emsp;&emsp;4）rtcm_slice.py：切分rtcm文件；
&emsp;&emsp;5）rtcm_decode.exe：rtcm解码程序；
&emsp;&emsp;6）RTKLIB_demo：PPP解算程序；
**文件结构**
&emsp;&emsp;根目录下：BHJD为测站子目录，logs为日志目录，SoluData为广播星历和精密星历的存储目录；
&emsp;&emsp;BHJD子目录下：rtknavi存储实时rtcm3文件，rtcm存储切片之后的rtcm文件，rinex存储解码后的obs文件，out存储PPP解算结果文件，logs存储解算日志；
![本地路径](<Directory Structure.PNG>)

## 2.使用方法

&emsp;&emsp;修改测站子目录下的conf配置文件，个性化配置rt_stream32_lowcpu2.py中站点参数，一键启动start PPP.bat。
