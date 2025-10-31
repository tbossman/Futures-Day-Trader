
  # ---- Setup: packages ----
pkgs <- c("ggplot2", "ggpubr", "dplyr", "readr")
need <- pkgs[!(pkgs %in% installed.packages()[,"Package"])]
if (length(need)) install.packages(need, quiet = TRUE)

library(ggplot2)
library(ggpubr)
library(dplyr)
library(readr)

# ---- Load data ----
# If your CSV is inside a "logs" folder, keep this. Otherwise, change the path.
data_path <- "logs/trades.csv"   # <- change if needed (e.g., "logs_file.csv" or "trades.csv")
df <- read_csv(data_path, show_col_types = FALSE)

# Optional: parse timestamp for time-based plots
df <- df %>%
  mutate(
    ts = as.POSIXct(ts, format = "%Y-%m-%dT%H:%M:%OS", tz = "UTC"),
    is_win = pnl > 0
  )

head(df)

# ---- Win/Loss stats ----
total_trades <- nrow(df)
wins  <- sum(df$is_win, na.rm = TRUE)
losses <- total_trades - wins
win_loss_ratio <- if (losses == 0) Inf else wins / losses
win_pct <- wins / total_trades

cat("Total trades:", total_trades, "\n")
cat("Wins:", wins, "\n")
cat("Losses:", losses, "\n")
cat("Win/Loss ratio:", round(win_loss_ratio, 3), "\n")
cat("Win %:", scales::percent(win_pct), "\n")

# ---- Win vs Loss bar chart ----
win_counts <- df %>%
  summarize(Wins = sum(is_win), Losses = sum(!is_win)) %>%
  tidyr::pivot_longer(everything(), names_to = "Outcome", values_to = "Count")

ggplot(win_counts, aes(Outcome, Count)) +
  geom_col(width = 0.6) +
  geom_text(aes(label = Count), vjust = -0.4, size = 5) +
  labs(title = "Win vs Loss Count", x = NULL, y = "Trades") +
  theme_minimal(base_size = 12)

# ---- Correlation: entry vs exit (regression line + equation) ----
# If your file uses different column names, adjust aes(x= , y= )
ggplot(df, aes(x = entry, y = exit)) +
  geom_point() +
  geom_smooth(method = "lm", se = FALSE) +
  stat_cor(method = "pearson",
           label.x = min(df$entry, na.rm = TRUE),
           label.y = max(df$exit,  na.rm = TRUE)) +
  stat_regline_equation(
    label.x = min(df$entry, na.rm = TRUE),
    label.y = max(df$exit,  na.rm = TRUE) * 0.99
  ) +
  labs(title = "Entry vs Exit: Correlation & Regression",
       x = "Entry", y = "Exit") +
  theme_minimal(base_size = 12)

# ---- (Optional) Equity over time line graph ----
# Requires parsed 'ts' above
ggplot(df, aes(x = ts, y = equity)) +
  geom_line() +
  geom_point() +
  labs(title = "Equity Over Time", x = "Time", y = "Equity") +
  theme_minimal(base_size = 12)

# ---- PnL by Trade Number with Best-Fit Line ----

# Add trade number column
# ---- Cumulative PnL over Trades ----

# Add trade number
df$trade_number <- seq_len(nrow(df))

# Calculate cumulative pnl
df$cum_pnl <- cumsum(df$pnl)

ggplot(df, aes(x = trade_number, y = cum_pnl)) +
  geom_hline(yintercept = 0, color = "red", linetype = "dashed") +   # break-even line
  geom_point(color = "blue", size = 3) +                             # cumulative points
  geom_line(color = "blue", alpha = 0.6) +                           # connect cumulative path
  geom_smooth(method = "lm", se = FALSE, color = "black") +          # regression (trend) line
  stat_regline_equation(                                            # equation of trend
    aes(label = ..eq.label..),
    label.x = 1,
    label.y = max(df$cum_pnl, na.rm = TRUE) * 0.95
  ) +
  labs(title = "Cumulative Profit/Loss Over Trades",
       x = "Trade Number",
       y = "Cumulative PnL") +
  theme_minimal(base_size = 12) 
  #coord_cartesian(ylim = c(min(df$pnl, na.rm = TRUE) - 5,
   #                        max(df$pnl, na.rm = TRUE) + 5))