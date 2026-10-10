"use strict";

// Retirement guard only: preserve other model IDs and their existing alias mapping.
function isRetiredModel(model) {
  if (typeof model !== 'string') return false;
  const id = model.trim().toLowerCase();
  // The exact legacy ID below was previously the retired alias's mapping target.
  return /^doubao-seedance-1[.-]5(?:[.-]pro)?(?:-.*)?$/.test(id) ||
    id === 'doubao-seedance-1-0-pro-250528';
}
function assertActiveModel(model) {
  if (isRetiredModel(model)) {
    throw new Error('Seedance 1.5 Pro 已下线，不能创建任务或保存为偏好；请选择 Seedance 2.0、2.0 Fast、2.0 Mini 或 2.5。');
  }
}
module.exports = { isRetiredModel, assertActiveModel };
