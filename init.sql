/*
agent基线测试系统，用于测试agent的任务完成率
第一个版本基于 OpenAI Responses API 格式 进行开发，端点为：/v1/responses
*/

create table if not exists ebase.users (
id int not null auto_increment primary key,
username char(32) not null unique,	    -- 用户名
password varchar(128) not null,	        -- 密码(加密后的)
email varchar(128),                     -- 邮箱
privilege int not null default 0		-- 权限等级，0为普通用户，1为管理员, 2为超级管理员
);
/*
0具备测试权限，
1具备管理任务和任务组权限，
2具备全部权限
*/

create table if not exists ebase.task_group (
id int not null auto_increment primary key,
name char(32),              
description varchar(128),   -- 描述组行为
variables TEXT              -- 组内变量，表示该组内所有任务共享的变量，字段类型为json，但以字符串形式存储，多个变量之间用逗号分隔
);
/*
任务组，一个任务组内包含多个任务，任务组的作用是将多个任务组合在一起，方便管理和执行。每个任务组可以有一个名称和描述，用于标识和说明该组的用途。
variables的格式类型：
{
    "var1": "value1",
    "var2": "value2",
    "var3": {
        "subvar1": "subvalue1",
        "subvar2": "subvalue2"
    }
    ...
}
*/

create table if not exists ebase.task_list(
tid int not null primary key,   -- task id
gid int not null,			    -- group id
name char(32) ,				    -- 任务名称
description varchar(128),       -- 描述任务行为
depends varchar(256),           -- 依赖任务id（只能依赖组内任务），表示该任务依赖于其他任务的执行结果，只有在依赖任务执行成功后，才能执行该任务，字段类型为列表，但以字符串形式存储，多个依赖任务id之间用逗号分隔
pre_script varchar(128),        -- 前置条件脚本地址入口，用户可以选择在线编写脚本或上传脚本，脚本将由系统保存到服务器。
input TEXT,                     -- 输入参数，应为自然语言，该参数将作为提示词发送给agent
expect varchar(128),            -- 期望输出结果校验脚本，用户可以根据实际情况编写期望输出结果校验脚本，用于判断任务是否执行成功，返回值约定：0为任务成功，1为任务失败
clean varchar(128)              -- 环境清理脚本地址入口，用户可以选择在线编写脚本或上传脚本，脚本将由系统保存到服务器。
);
/*
任务列表
每个任务执行流程为：
1. 检查依赖任务是否执行成功，如果有依赖任务且未执行成功，则该任务无法执行。
2. 执行前置条件脚本，如果前置条件脚本执行失败，则该任务无法执行。
3. 执行任务，如果任务执行失败，则记录失败信息，并将任务状态设置为执行失败。
4. 如果任务执行成功，则记录成功信息，并将任务状态设置为执行成功。

任务前置条件的配置过程可能比较复杂，用户需要根据实际情况编写前置条件脚本，并将脚本地址配置到任务列表中。
前置条件脚本可以是任何可执行的脚本文件，例如shell脚本、python脚本等，用户需要确保脚本能够在agent上正确执行，并返回正确的执行结果。

对于input字段，保存在数据库中的方式为JSON格式：
{
    "prompt": "用户输入的自然语言提示，可以使用 {VARIABLE_NAME} 的方式引用任务组内的变量，对于多级嵌套的变量，可以使用 {VARIABLE_NAME.SUB_VARIABLE_NAME} 的方式引用，任务执行时由系统自动填充变量",
    "conversation": [
        {"field": "如果任务需要多轮对话，则使用conversation字段，匹配field的内容，自动回复anwser中存储的内容，列表中的每个对象只会被触发一次", "answer":"回答的内容"},
        {... ...}
    ]
}
*/

create table if not exists ebase.subjects (
id      int not null auto_increment    primary key,					-- subject id
ipaddr 	char(32)      not null default '127.0.0.1',	                -- agent ipaddr
port	int           not	null default 8642,			            -- agent port
secret	varchar(128)  not null default '',			                -- apikey
api_mode int not null default 0					                -- api模式，0为OpenAI Responses API模式，其他类型待定
);
/*
待测主体列表，包含待测试的agent的ip地址、端口号和apikey等信息，每次执行任务时，系统会根据该列表中的信息连接到对应的agent，并执行相应的测试任务。
用户执行测试时，应该选择一个待测agent，然后从task_list中选择要对该agent执行的任务，系统会将任务发送给该agent，并等待其返回执行结果。
*/

create table if not exists ebase.task_batch (
id int not null auto_increment primary key,		-- 批次id
sid int not null,                               -- subject id
gid int not null,                               -- group id
status int not null default 0,                  -- 批次状态，0为未执行，1为执行中，2为已完成，3为执行失败, 4为部分成功
create_time timestamp default current_timestamp	-- 批次创建时间
);
/*
每次测试时创建一个测试批次，用户完成测试任务创建后生成这个测试批次，系统会将该批次的状态设置为未执行。用户可以通过查询该表来查看某个批次的状态，并根据状态进行相应的处理和分析。
用户可以选择某个批次的任务执行下列操作：
1. 执行批次任务：系统会根据该批次的任务列表，依次执行每个任务，并将执行结果存储在task_result表中。
2. 查看批次任务执行结果：用户可以通过查询task_result表来查看某个批次的任务执行结果，并根据结果进行相应的处理和分析。
3. 重新执行批次任务：用户可以选择重新执行某个批次的任务，系统会根据该批次的任务列表，依次执行每个任务，并将执行结果存储在task_result表中，覆盖原有的执行结果。
4. 删除批次任务：用户可以选择删除某个批次的任务，系统会将该批次的任务从task_batch表中删除，并将该批次的任务执行结果从task_result表中删除。
5. 导出批次任务执行结果：用户可以选择导出某个批次的任务执行结果，系统会将该批次的任务执行结果导出为CSV文件，供用户下载和分析。
6. 修改批次任务：用户可以选择修改某个批次的任务，系统会根据用户的修改，更新该批次的任务列表，并将该批次的任务执行结果从task_result表中删除。

用户可以选择某个任务组来执行测试任务，也可以选择某个任务组内的部分任务来执行，如果选择了部分任务，则系统会根据任务的依赖关系，自动选择需要执行的任务，并将其加入到批次任务中。
*/

create table if not exists ebase.task_result (
id int not null auto_increment primary key,		-- result id
sid int not null,					            -- subject id
gid int not null,					            -- group id
tid int not null,					            -- task id
result int not null default 0,                  -- 执行状态，0为未执行，1为执行成功，2为执行失败
process TEXT,                                   -- 执行结果信息，包含任务执行的详细日志和输出结果
create_time timestamp default current_timestamp	-- 执行时间
);
/*
测试完成后，系统会将测试信息存储在task_result表中。
用户可以通过查询该表来查看某个任务的测试结果，并根据结果进行相应的处理和分析。
*/

