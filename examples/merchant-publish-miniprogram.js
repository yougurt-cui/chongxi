// Token 使用现有小程序登录接口返回值。调用方负责从自己的登录状态中传入。
const BASE_URL = 'https://www.chongxi.cloud';

function request(token, path, {method = 'GET', data, idempotencyKey} = {}) {
  return new Promise((resolve, reject) => {
    wx.request({url: BASE_URL + path, method, data,
      header: {Authorization: `Bearer ${token}`, ...(idempotencyKey ? {'Idempotency-Key': idempotencyKey} : {})},
      success(res) {
        if (res.statusCode >= 200 && res.statusCode < 300 && res.data.code === 0) resolve(res.data.data);
        else reject(new Error(res.data.message || '请求失败'));
      }, fail: reject});
  });
}

function uploadImage(token, filePath) {
  return new Promise((resolve, reject) => {
    wx.uploadFile({url: BASE_URL + '/api/images/upload', filePath, name: 'file',
      header: {Authorization: `Bearer ${token}`},
      success(res) {
        try {
          const body = JSON.parse(res.data);
          if (res.statusCode === 201 && body.code === 0) resolve(body.data);
          else reject(new Error(body.message || '图片上传失败'));
        } catch (error) { reject(error); }
      }, fail: reject});
  });
}

// 一次发布时生成 requestKey 并保留；网络失败后的重试继续使用同一个 key。
// 多商家账号先通过 getMerchantIdentity 选择 merchant_id，再传给此函数。
async function publishProduct(token, filePaths, product, requestKey) {
  const uploaded = await Promise.all(filePaths.map(path => uploadImage(token, path)));
  return request(token, '/api/merchant/products/publish', {
    method: 'POST', idempotencyKey: requestKey,
    data: {...product, image_ids: uploaded.map(image => image.image_id)}
  });
}

module.exports = {
  uploadImage, publishProduct,
  getMerchantIdentity: token => request(token, '/api/merchant/identity'),
  getMyProducts: (token, page = 1) => request(token, `/api/merchant/products/mine?page=${page}`),
  bindProduct: (token, productId, postId) => request(token, `/api/merchant/products/${encodeURIComponent(productId)}/bind`, {method: 'POST', data: {post_id: postId}})
};
