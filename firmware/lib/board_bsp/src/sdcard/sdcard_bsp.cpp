#include <stdio.h>
#include <string.h>
#include <Arduino.h>
#include <sys/unistd.h>
#include <sys/stat.h>
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"
#include "driver/sdmmc_host.h"
#include "sdcard_bsp.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define SDMMC_D0_PIN    GPIO_NUM_40
#define SDMMC_CLK_PIN   GPIO_NUM_39
#define SDMMC_CMD_PIN   GPIO_NUM_41

// 4-bit SDMMC mode is ~20% faster than the 1-bit mode this board has always
// used (confirmed against Espressif/community benchmarks -- NOT the ~4x a
// naive "4x the data lines" guess would suggest), but requires the SD
// card's D1/D2/D3 lines to actually be wired to GPIOs on this board, which
// this custom PCB's own pin map (user_config.h) has never defined -- only
// D0/CLK/CMD exist there today. Left OFF by default (SDMMC_TRY_4BIT 0) so
// this file's behavior is completely unchanged until someone confirms
// those traces exist and fills in the real GPIO numbers below. If/when
// enabled, sdcard_init() below still automatically falls back to the
// known-working 1-bit config at runtime if the 4-bit mount fails for any
// reason (wrong/missing pins, bad wiring) -- see sdcard_init()'s retry.
#define SDMMC_TRY_4BIT 0
#if SDMMC_TRY_4BIT
  // TODO: replace with this board's actual SD D1/D2/D3 GPIO numbers (from
  // the schematic) before flipping SDMMC_TRY_4BIT to 1 -- these are
  // placeholders and do NOT reflect confirmed wiring.
  #define SDMMC_D1_PIN    GPIO_NUM_NC
  #define SDMMC_D2_PIN    GPIO_NUM_NC
  #define SDMMC_D3_PIN    GPIO_NUM_NC
#endif

#define SDlist "/sdcard" //Directory, similar to a standard

sdmmc_card_t *card_host = NULL;

static bool mountSd(sdmmc_host_t &host, int width)
{
  esp_vfs_fat_sdmmc_mount_config_t mount_config = {};
  	mount_config.format_if_mount_failed = false;       //如果挂靠失败，创建分区表并格式化SD卡
  	mount_config.max_files = 5;                        //打开文件最大数
  	mount_config.allocation_unit_size = 16 * 1024 *3;  //类似扇区大小

  sdmmc_slot_config_t slot_config = SDMMC_SLOT_CONFIG_DEFAULT();
  	slot_config.width = width;
  	slot_config.clk = SDMMC_CLK_PIN;
  	slot_config.cmd = SDMMC_CMD_PIN;
  	slot_config.d0  = SDMMC_D0_PIN;
#if SDMMC_TRY_4BIT
  if (width == 4) {
    slot_config.d1 = SDMMC_D1_PIN;
    slot_config.d2 = SDMMC_D2_PIN;
    slot_config.d3 = SDMMC_D3_PIN;
  }
#endif
  ESP_ERROR_CHECK_WITHOUT_ABORT(esp_vfs_fat_sdmmc_mount(SDlist, &host, &slot_config, &mount_config, &card_host));
  return card_host != NULL;
}

void sdcard_init(void)
{
  sdmmc_host_t host = SDMMC_HOST_DEFAULT();
  host.max_freq_khz = SDMMC_FREQ_HIGHSPEED;//高速

#if SDMMC_TRY_4BIT
  Serial.println("sdcard_bsp: attempting 4-bit SDMMC mode...");
  if (!mountSd(host, 4)) {
    Serial.println("sdcard_bsp: 4-bit mount failed (wiring not confirmed?) -- rolling back to known-working 1-bit mode");
    mountSd(host, 1);
  }
#else
  mountSd(host, 1);
#endif

  if(card_host != NULL)
  {
    sdmmc_card_print_info(stdout, card_host); //Print out the card_host information
    Serial.print("practical_size:");
    Serial.println(sdcard_GetValue());  //g
  }
}
bool sdcard_is_mounted(void)
{
  return card_host != NULL && sdmmc_get_status(card_host) == ESP_OK;
}

float sdcard_GetValue(void)
{
  if(card_host != NULL)
  {
    return (float)(card_host->csd.capacity)/2048/1024; //G
  }
  else
  return 0;
}

/*write data
path:path
data:data
*/
esp_err_t s_example_write_file(const char *path, char *data)
{
  esp_err_t err;
  if(card_host == NULL)
  {
    return ESP_ERR_NOT_FOUND;
  }
  err = sdmmc_get_status(card_host); //First check if there is an SD card_host
  if(err != ESP_OK)
  {
    return err;
  }
  FILE *f = fopen(path, "w"); //Get path address
  if(f == NULL)
  {
    printf("path:Write Wrong path\n");
    return ESP_ERR_NOT_FOUND;
  }
  fprintf(f, data); //write in
  fclose(f);
  return ESP_OK;
}
/*
read data
path:path
*/
esp_err_t s_example_read_file(const char *path,char *pxbuf,uint32_t *outLen)
{
  esp_err_t err;
  if(card_host == NULL)
  {
    printf("path:card_host == NULL\n");
    return ESP_ERR_NOT_FOUND;
  }
  err = sdmmc_get_status(card_host); //First check if there is an SD card_host
  if(err != ESP_OK)
  {
    printf("path:card_host == NO\n");
    return err;
  }
  FILE *f = fopen(path, "rb");
  if (f == NULL)
  {
    printf("path:Read Wrong path\n");
    return ESP_ERR_NOT_FOUND;
  }
  fseek(f, 0, SEEK_END);     //Move the pointer to the back
  uint32_t unlen = ftell(f);
  //fgets(pxbuf, unlen, f); //Read text
  fseek(f, 0, SEEK_SET); //Move the pointer to the front
  uint32_t poutLen = fread((void *)pxbuf,1,unlen,f);
  printf("pxlen: %ld,outLen: %ld\n",unlen,poutLen);
  //*outLen = poutLen;
  fclose(f);
  return ESP_OK;
}
