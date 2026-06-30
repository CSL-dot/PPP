clc
clear

while true

    % ===== 当前UTC时间 =====
    t = datetime('now','TimeZone','UTC');

    % ===== 年积日 =====
    doy = day(t,'dayofyear');

    % ===== 构造文件名 =====
    filename = sprintf('BRDM%03d0.rnx',doy);

    % ===== 下载地址 =====
    url = ['https://iod.navfirst.com/brdc/' filename];

    % ===== 本地路径 =====
    savefile = fullfile(pwd,filename);

    try

        websave(savefile,url);

        fprintf('BRDC更新成功: %s  %s\n',filename,datestr(now));

    catch

        fprintf('下载失败: %s\n',filename);

    end

    % ===== 5分钟更新 =====
    pause(300)

end
