CREATE TABLE `tcg_checkpoints` (
	`owner` text NOT NULL,
	`thread` text NOT NULL,
	`ns` text NOT NULL,
	`id` text NOT NULL,
	`parent` text,
	`payload` text NOT NULL,
	`metadata` text NOT NULL,
	PRIMARY KEY(`owner`, `thread`, `ns`, `id`)
);
--> statement-breakpoint
CREATE TABLE `tcg_events` (
	`id` integer PRIMARY KEY AUTOINCREMENT NOT NULL,
	`owner` text NOT NULL,
	`run_id` text NOT NULL,
	`kind` text NOT NULL,
	`payload` text NOT NULL,
	`created_at` text NOT NULL
);
--> statement-breakpoint
CREATE INDEX `tcg_event_run` ON `tcg_events` (`owner`,`run_id`,`id`);--> statement-breakpoint
CREATE TABLE `tcg_leases` (
	`owner` text NOT NULL,
	`run_id` text NOT NULL,
	`token` text NOT NULL,
	`expires` integer NOT NULL,
	PRIMARY KEY(`owner`, `run_id`)
);
--> statement-breakpoint
CREATE TABLE `tcg_owners` (
	`owner` text PRIMARY KEY NOT NULL,
	`revision` integer DEFAULT 0 NOT NULL
);
--> statement-breakpoint
CREATE TABLE `tcg_records` (
	`owner` text NOT NULL,
	`id` text NOT NULL,
	`kind` text NOT NULL,
	`project_id` text,
	`chat_id` text,
	`payload` text NOT NULL,
	PRIMARY KEY(`owner`, `id`)
);
--> statement-breakpoint
CREATE INDEX `tcg_scope` ON `tcg_records` (`owner`,`kind`,`project_id`,`chat_id`);--> statement-breakpoint
CREATE TABLE `tcg_writes` (
	`owner` text NOT NULL,
	`thread` text NOT NULL,
	`ns` text NOT NULL,
	`checkpoint` text NOT NULL,
	`task` text NOT NULL,
	`idx` integer NOT NULL,
	`channel` text NOT NULL,
	`payload` text NOT NULL,
	PRIMARY KEY(`owner`, `thread`, `ns`, `checkpoint`, `task`, `idx`)
);
